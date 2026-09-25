"""Phase 33 (delegate-card admin actions) — «🔍 Сверить с БД», раздел «📊 Данные», рядом с
«🔄 Синхронизация»/«♻️ Пересобрать таблицу» (тот же класс операции: ходит в живой Google API,
поэтому редрей — свой раздел, не корень, `op_return_keyboard`, тот же приём, что
`handlers/admin_sheets.py`). Право — `settings` (то же, что у соседних двух кнопок); с учётом
города — менеджер, привязанный к городу, сверяет только свой (`_admin_city_view`, тот же
резолвер, что у «📄 Экспорт CSV»).

Сама логика — `services/sheet_reconcile.py` (aiogram-free). Этот модуль только строит текст/
клавиатуры и вызывает её.

Шов той же формы, что соседние Phase 33 (`admin_city_move.py`/`admin_resume_replace.py`):
своего `Router()` нет, хендлеры декорируют ОБЩИЙ `admin.router`, модуль импортируется ХВОСТОМ
`handlers/admin.py` (golden snapshot: чистая вставка)."""
import html as html_module
import logging
from collections import Counter

from aiogram import F, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from handlers.admin_core import _admin_city_view
from services.decision_delivery import resend_undelivered_decisions
from services.sheet_reconcile import (
    apply_append_missing,
    apply_fix_statuses,
    build_report,
    chunk_report_lines,
    render_report_lines,
    report_to_csv_bytes,
)

logger = logging.getLogger(__name__)


def _tab_label(tab) -> str:
    return tab if tab is not None else "главная"


def _crash_text(done: int, total: int) -> str:
    """Тот же посыл, что у соседнего `sync_sheet` (handlers/admin_sheets.py): неожиданный сбой
    (Sheets API/БД не ответили) ловится, а не роняет хендлер молча — но, в отличие от sync_sheet,
    здесь важно сказать, сколько реально успело записаться ДО сбоя (apply_append_missing/
    apply_fix_statuses несут это в `done`/`total` даже при `ok=False, crashed=True`)."""
    if done:
        return f"❌ Не получилось: таблица не ответила. Попробуйте позже — записано {done} из {total} (что успели)."
    return "❌ Не получилось: таблица не ответила. Попробуйте позже — ничего не записано."


def _breakdown(items: list[dict]) -> list[tuple[str, int]]:
    counts = Counter(it["tab"] for it in items)
    ordered = sorted(counts.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    return [(_tab_label(t), n) for t, n in ordered]


async def _action_keyboard(admin_id: int, report: dict) -> InlineKeyboardMarkup:
    from handlers.admin_sections import op_return_keyboard  # ленивый шов

    rows: list[list[InlineKeyboardButton]] = []
    if report["ok"] and report["missing_rows"]:
        rows.append([InlineKeyboardButton(
            text="➕ Дописать недостающие строки", callback_data="sheetrec_append_confirm",
        )])
    if report["ok"] and report["status_mismatch"]:
        rows.append([InlineKeyboardButton(
            text="🔄 Выправить статусы", callback_data="sheetrec_status_confirm",
        )])
    # Координатор 25.09: переотправка НЕ зависит от Google Sheets (apply_decision_effects(...,
    # sheet=False)) — кнопка показывается и когда report["ok"] is False (таблица недоступна),
    # раскладка decision_delivery посчитана над БД независимо от снимка листа.
    if (report.get("decision_delivery") or {}).get("resendable"):
        rows.append([InlineKeyboardButton(
            text="📨 Переотправить решения", callback_data="sheetrec_resend_confirm",
        )])
    rows.append([InlineKeyboardButton(text="📥 Полный список (CSV)", callback_data="sheetrec_csv")])
    base = await op_return_keyboard(admin_id, "admin_sheet_reconcile")
    return InlineKeyboardMarkup(inline_keyboard=rows + base.inline_keyboard)


@router.callback_query(F.data == "admin_sheet_reconcile")
async def sheet_reconcile_open(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    await callback.answer("🔍 Сверяю...")
    await callback.message.edit_text("🔍 Читаю таблицу и базу…", parse_mode="HTML")

    scope, label = await _admin_city_view(admin_id)
    try:
        report = await build_report(city_scope=scope)
        lines = render_report_lines(report, city_label=label)
        chunks = chunk_report_lines(lines)

        await callback.message.edit_text(chunks[0], parse_mode="HTML")
        for chunk in chunks[1:]:
            await callback.message.answer(chunk, parse_mode="HTML")

        kb = await _action_keyboard(admin_id, report)
        await callback.message.answer("Что дальше:", reply_markup=kb)
    except Exception as e:
        logger.error(f"sheet_reconcile_open failed: {e}")
        await callback.message.edit_text(_crash_text(0, 0), parse_mode="HTML")


@router.callback_query(F.data == "sheetrec_csv")
async def sheet_reconcile_csv(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, label = await _admin_city_view(admin_id)
    try:
        report = await build_report(city_scope=scope)
        csv_bytes = report_to_csv_bytes(report)
        filename = "sheet_reconcile.csv" if scope is None else f"sheet_reconcile_{scope[0]}.csv"
        caption = "Полный список расхождений" + (f" — {html_module.escape(label)}" if label else "")
        document = BufferedInputFile(csv_bytes, filename=filename)
        await callback.message.answer_document(document, caption=caption)
        await callback.answer()
    except Exception as e:
        logger.error(f"sheet_reconcile_csv failed: {e}")
        await callback.answer(_crash_text(0, 0), show_alert=True)


@router.callback_query(F.data == "sheetrec_append_confirm")
async def sheet_reconcile_append_confirm(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, _label = await _admin_city_view(admin_id)
    try:
        report = await build_report(city_scope=scope)
    except Exception as e:
        logger.error(f"sheet_reconcile_append_confirm failed: {e}")
        await callback.answer(_crash_text(0, 0), show_alert=True)
        return
    if not report["ok"]:
        await callback.answer(f"❌ {report['error'] or 'таблица недоступна'}", show_alert=True)
        return
    if not report["missing_rows"]:
        await callback.answer("Уже нечего дописывать — пересчитал заново.", show_alert=True)
        return

    total = len(report["missing_rows"])
    parts = ", ".join(f"«{html_module.escape(tab)}» — {n}" for tab, n in _breakdown(report["missing_rows"]))
    text = (
        f"➕ <b>Дописать {total} недостающих строк</b> на вкладки: {parts}.\n\n"
        "Вкладки не создаются — только на уже существующие. Каждая строка пишется отдельно, "
        "с паузой; сбой одной не остановит остальные."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, дописать", callback_data="sheetrec_append_go")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="admin_sheet_reconcile")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "sheetrec_append_go")
async def sheet_reconcile_append_go(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, _label = await _admin_city_view(admin_id)
    await callback.answer("➕ Дописываю...")
    result = None
    try:
        result = await apply_append_missing(city_scope=scope)
        if not result["ok"]:
            if result.get("crashed"):
                text = _crash_text(result.get("done", 0), result.get("total", 0))
            else:
                text = f"❌ {html_module.escape(str(result['error']))}"
            await callback.message.edit_text(text, parse_mode="HTML")
            return

        lines = [f"✅ Дописано: <b>{result['done']}</b>"]
        if result["failed"]:
            lines.append(f"Не удалось: <b>{len(result['failed'])}</b>")
            for it in result["failed"][:10]:
                tab = html_module.escape(_tab_label(it["tab"]))
                reason = html_module.escape(str(it.get("reason") or "-"))
                lines.append(f"  id {it['tid']} на «{tab}»: {reason}")
            if len(result["failed"]) > 10:
                lines.append(f"  …и ещё {len(result['failed']) - 10}")
        kb = await _action_keyboard(admin_id, await build_report(city_scope=scope))
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        # Дописать успело до сбоя (сам apply_append_missing уже отловил бы это и вернул
        # crashed=True — сюда попадает только сбой ПОСЛЕ успешного apply, например обновление
        # клавиатуры повторным build_report; result тогда уже несёт настоящий done/total).
        logger.error(f"sheet_reconcile_append_go failed: {e}")
        done = result.get("done", 0) if result else 0
        total = result.get("total", 0) if result else 0
        await callback.message.edit_text(_crash_text(done, total), parse_mode="HTML")


@router.callback_query(F.data == "sheetrec_status_confirm")
async def sheet_reconcile_status_confirm(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, _label = await _admin_city_view(admin_id)
    try:
        report = await build_report(city_scope=scope)
    except Exception as e:
        logger.error(f"sheet_reconcile_status_confirm failed: {e}")
        await callback.answer(_crash_text(0, 0), show_alert=True)
        return
    if not report["ok"]:
        await callback.answer(f"❌ {report['error'] or 'таблица недоступна'}", show_alert=True)
        return
    if not report["status_mismatch"]:
        await callback.answer("Уже нечего выправлять — пересчитал заново.", show_alert=True)
        return

    total = len(report["status_mismatch"])
    parts = ", ".join(f"«{html_module.escape(tab)}» — {n}" for tab, n in _breakdown(report["status_mismatch"]))
    text = (
        f"🔄 <b>Выправить статус {total} строк</b> на вкладках: {parts}.\n\n"
        "Запись пойдёт тем же путём, что решение модератора. Только строки, где на вкладке "
        "ровно одна строка делегата — дубли не трогаем."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, выправить", callback_data="sheetrec_status_go")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="admin_sheet_reconcile")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "sheetrec_status_go")
async def sheet_reconcile_status_go(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, _label = await _admin_city_view(admin_id)
    await callback.answer("🔄 Выправляю...")
    result = None
    try:
        result = await apply_fix_statuses(city_scope=scope)
        if not result["ok"]:
            if result.get("crashed"):
                text = _crash_text(result.get("done", 0), result.get("total", 0))
            else:
                text = f"❌ {html_module.escape(str(result['error']))}"
            await callback.message.edit_text(text, parse_mode="HTML")
            return

        lines = [f"✅ Выправлено: <b>{result['done']}</b>"]
        if result["failed"]:
            lines.append(f"Не удалось: <b>{len(result['failed'])}</b>")
            for it in result["failed"][:10]:
                tab = html_module.escape(_tab_label(it["tab"]))
                reason = html_module.escape(str(it.get("reason") or "-"))
                lines.append(f"  id {it['tid']} на «{tab}»: {reason}")
            if len(result["failed"]) > 10:
                lines.append(f"  …и ещё {len(result['failed']) - 10}")
        kb = await _action_keyboard(admin_id, await build_report(city_scope=scope))
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"sheet_reconcile_status_go failed: {e}")
        done = result.get("done", 0) if result else 0
        total = result.get("total", 0) if result else 0
        await callback.message.edit_text(_crash_text(done, total), parse_mode="HTML")


# ── Координатор 25.09: «📨 Переотправить решения» ────────────────────────────────────────────
#
# Право — то же, что у остальных кнопок этого экрана («settings», wildcard `sheetrec_*` в
# handlers/admin_caps.py уже покрывает новые callback'ы автоматически). НЕ `moderate_reg`:
# переотправка — операция над «Сверить с БД» (учёт/техническое обслуживание записей решения),
# не сама модерация заявок — тот же класс права, что у «🔄 Синхронизация»/«♻️ Пересобрать».

@router.callback_query(F.data == "sheetrec_resend_confirm")
async def sheet_reconcile_resend_confirm(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, _label = await _admin_city_view(admin_id)
    try:
        report = await build_report(city_scope=scope)
    except Exception as e:
        logger.error(f"sheet_reconcile_resend_confirm failed: {e}")
        await callback.answer(_crash_text(0, 0), show_alert=True)
        return

    dd = report.get("decision_delivery") or {}
    resendable = dd.get("resendable", [])
    blocked = dd.get("blocked", [])
    if not resendable:
        await callback.answer("Уже нечего переотправлять — пересчитал заново.", show_alert=True)
        return

    counts = Counter(it["decision"] for it in resendable)
    text = (
        f"📨 <b>Переотправить решения {len(resendable)} делегатам</b> "
        f"(одобрено {counts.get('approved', 0)}, отклонено {counts.get('rejected', 0)}).\n\n"
        "Текст — тот же, что при решении, для каждого по его ТЕКУЩЕМУ статусу, городу и языку "
        "(если статус успел смениться до отправки — письмо не уйдёт двойным, перед отправкой "
        "всё пересчитывается заново).\n\n"
        "Повторно шлём только текст решения — шаг оплаты и бонус заново не открываются."
    )
    if blocked:
        text += (
            f"\n\nЗаблокировавшим бота ({len(blocked)}) не шлём — им нужно написать вручную "
            "(список — в полном отчёте и в CSV)."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, переотправить", callback_data="sheetrec_resend_go")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="admin_sheet_reconcile")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "sheetrec_resend_go")
async def sheet_reconcile_resend_go(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    scope, _label = await _admin_city_view(admin_id)
    await callback.answer("📨 Переотправляю...")
    result = None
    try:
        result = await resend_undelivered_decisions(callback.bot, city_scope=scope)
        if not result["ok"]:
            if result.get("crashed"):
                text = _crash_text(result.get("done", 0), result.get("total", 0))
            else:
                text = f"❌ {html_module.escape(str(result['error']))}"
            await callback.message.edit_text(text, parse_mode="HTML")
            return

        lines = [f"✅ Доставлено: <b>{result['done']}</b>"]
        if result["failed"]:
            lines.append(f"Не удалось: <b>{len(result['failed'])}</b>")
            for it in result["failed"][:10]:
                name = html_module.escape(str(it.get("name") or it["tid"]))
                reason = html_module.escape(str(it.get("reason") or "-"))
                lines.append(f"  {name}: {reason}")
            if len(result["failed"]) > 10:
                lines.append(f"  …и ещё {len(result['failed']) - 10}")
        if result.get("blocked"):
            lines.append(
                f"Бот заблокирован (не отправляли, напишите вручную): <b>{len(result['blocked'])}</b>"
            )
        kb = await _action_keyboard(admin_id, await build_report(city_scope=scope))
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"sheet_reconcile_resend_go failed: {e}")
        done = result.get("done", 0) if result else 0
        total = result.get("total", 0) if result else 0
        await callback.message.edit_text(_crash_text(done, total), parse_mode="HTML")
