"""«📋 Лист UR REGS» на экране «🏫 Делегации»: выбор вкладки, сухая сверка и включение записи.

Запись в вкладку делегаций — обдуманный шаг в два касания. Менеджер выбирает вкладку из
реального списка листа (по индексу, список лежит в FSM-данных), бот показывает сверку без
записи (`services.delegations_mirror.dry_run_sync`): сколько строк в листе, сколько узнал по
ID ответа, сколько добавит, совпадает ли шапка A–L с вопросами формы и свободна ли колонка M.
Только после этого появляется кнопка «✅ Включить запись …», а за ней — подтверждение, которое
называет, что именно изменится. Так первая запись не задваивает строки, уже выгруженные
в лист вручную из Яндекса.

Включение = `set_form_mirror(fid, tab, None)` + режим `yandex_export` + все ответы формы снова
в очередь листа (`requeue_form_answers`) — дальше их разбирает обычный `ext_forms_sheet_drain`.
Выключение возвращает режим `bot`, снимает вкладку и гасит предупреждение листа.

Шов на общий `handlers.admin.router` (импортируется из хвоста handlers/admin_delegations.py,
декораторы в одну строку). Права — `dlg_*` → `moderate_reg` (handlers/admin_caps.py).
Названия вкладок и ячейки шапки — чужой текст, в HTML только через `_e()`.
"""
import asyncio
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import ext_forms_db as ef
from handlers.admin import router
from handlers.admin_delegations import (
    _btn, _current_form, _cut, _e, _kb, _show, _tail_int, _to_screen, render_screen,
)
from services import delegations_mirror, sheets

logger = logging.getLogger(__name__)

PREFERRED_TAB = "UR REGS"

_SHEET_OFF = (
    "📋 <b>Лист для делегаций</b>\n\n"
    "Таблица недоступна — проверьте доступ бота к Google-таблице в «📊 Данные»."
)
_PICKER_TEXT = (
    "📋 <b>Лист для делегаций</b>\n\n"
    "Выберите вкладку, куда выгружали ответы из Яндекс Формы (обычно «UR REGS»). Бот будет "
    "обновлять в ней строки по ID ответа и вести колонку M «В боте». Перед первой записью "
    "покажу сверку."
)
_NO_TABS = "Подходящих вкладок в таблице нет — служебные вкладки бота в списке не показываются."
_STALE_TABS = "Список вкладок устарел — откройте выбор ещё раз"
_STALE_TAB = "Вкладка не выбрана — откройте «📋 Лист UR REGS» ещё раз"
_NOT_ENABLED = "Запись в лист сейчас выключена"
_TAB_GONE = "Вкладка «{tab}» не найдена — выберите заново."
_READ_FAILED = (
    "Не получилось прочитать лист «{tab}» — таблица сейчас недоступна. "
    "Попробуйте «🔁 Сверить ещё раз» через минуту."
)
_ZERO_MATCHED = (
    "⚠️ Ни одна строка не узнана по ID — похоже, ID в листе и в ответах формы разные. "
    "Запись добавит {new} новых строк рядом со старыми. Лучше сначала спросить разработчика."
)


def _form_enabled(form: dict) -> bool:
    return form.get("mirror_mode") == "yandex_export" and bool(form.get("mirror_tab"))


# ── выбор вкладки ─────────────────────────────────────────────────────────────────────────

async def _hidden_tabs(form: dict) -> set[str]:
    # Ленивый шов: модуль настройки форм живёт в том же роутере, импорт на уровне модуля
    # сдвинул бы порядок регистрации хендлеров.
    from handlers import admin_ext_forms_setup as setup
    try:
        return await setup.protected_tab_titles(form)
    except Exception:  # noqa: BLE001 — список служебных вкладок вторичен, выбор не должен падать
        logger.exception("delegations: служебные вкладки не прочитаны (form=%s)", form.get("id"))
        return set()


@router.callback_query(F.data == "dlg_sheet")
async def dlg_sheet(callback: types.CallbackQuery, state: FSMContext):
    form = await _current_form()
    if form is None:
        await render_screen(callback)
        await callback.answer()
        return
    titles = await sheets.list_worksheet_titles()
    if titles is None:
        await _show(callback, _SHEET_OFF, _kb([[_to_screen()]]))
        await callback.answer()
        return
    hidden = await _hidden_tabs(form)
    titles = [t for t in titles if t not in hidden]
    titles.sort(key=lambda t: t != PREFERRED_TAB)  # «UR REGS» — первой, остальные как в листе
    await state.update_data(dlg_tabs=list(titles))
    current = form.get("mirror_tab") if _form_enabled(form) else None
    rows = [[_btn(("✓ " if t == current else "") + _cut(t), f"dlg_tab:{i}")]
            for i, t in enumerate(titles)]
    text = _PICKER_TEXT if titles else f"{_PICKER_TEXT}\n\n{_NO_TABS}"
    rows.append([_to_screen()])
    await _show(callback, text, _kb(rows))
    await callback.answer()


# ── сухая сверка ──────────────────────────────────────────────────────────────────────────

async def _dry_run(form: dict, tab: str) -> dict | None:
    """Сверка вкладки с ответами формы без записи. Список ID — `known_answer_ids`: вместе с
    сохранёнными ответами в нём лежат и надгробия ответов, удалённых в Яндексе, — они попадут
    в «добавлю новых», хотя писатель их не пишет (см. 36-04). None — вкладки нет."""
    fid = int(form["id"])
    columns = await ef.list_columns(fid)
    answer_ids = sorted(await ef.known_answer_ids(fid))
    return await asyncio.to_thread(delegations_mirror.dry_run_sync, tab, columns, answer_ids)


def _header_line(res: dict) -> str:
    diff = res.get("header_diff") or []
    if not diff:
        return "Шапка A–L: ✅ совпадает с вопросами формы"
    parts = "; ".join(f"{_e(col)} «{_e(sheet)}» ≠ «{_e(form)}»" for col, sheet, form in diff)
    return f"Шапка A–L: ⚠️ отличается: {parts}"


def _m_line(res: dict) -> str:
    m1 = res.get("m_header") or ""
    if not m1:
        return "Колонка M: ✅ свободна"
    if res.get("m_free"):
        return f"Колонка M: ✅ уже «{_e(delegations_mirror.M_HEADER)}»"
    return f"Колонка M: ⛔ занята («{_e(m1)}») — освободите её"


def _check_screen(res: dict | None, tab: str, form: dict) -> tuple[str, InlineKeyboardMarkup]:
    if res is None:
        return _TAB_GONE.format(tab=_e(tab)), _kb([[_btn("📋 Выбрать вкладку", "dlg_sheet")],
                                                  [_to_screen()]])
    matched, new = int(res.get("matched") or 0), int(res.get("new") or 0)
    rows_n = int(res.get("sheet_rows") or 0)
    lines = [
        f"🔍 <b>Сверка с листом «{_e(tab)}»</b>\n",
        f"В листе строк: {rows_n}",
        f"Узнал по ID ответа: {matched}",
        f"Добавлю новых: {new}",
        f"Не узнал в листе (строки без ответа в боте): {int(res.get('unknown_sheet_ids') or 0)}",
        _header_line(res),
        _m_line(res),
    ]
    colours = (f"Серых строк (не ЦА): {int(res.get('grey_rows') or 0)} · "
               f"зелёных (не трогаю): {int(res.get('green_rows') or 0)}")
    if not res.get("colours_ok", True):
        colours += " · цвета не прочитаны"
    lines.append(colours)
    dups = int(res.get("duplicate_sheet_ids") or 0)
    if dups:
        lines.append(f"Повторяющихся ID в листе: {dups} — обновлю первую из строк")
    extra = res.get("extra_questions") or []
    if extra:
        lines.append("⚠️ Вопросы формы без колонки: " + ", ".join(f"«{_e(q)}»" for q in extra))
    if form.get("mirror_warning"):
        lines.append(f"⚠️ {_e(form['mirror_warning'])}")
    if matched == 0 and rows_n > 0:
        lines.append("\n" + _ZERO_MATCHED.format(new=new))
    enabled = _form_enabled(form)
    if enabled and form.get("mirror_tab") != tab:
        lines.append(f"\nСейчас запись идёт в «{_e(form['mirror_tab'])}».")
    elif enabled:
        lines.append("\nЗапись в эту вкладку включена.")

    kb_rows: list[list[InlineKeyboardButton]] = []
    if res.get("m_free"):
        kb_rows.append([_btn(f"✅ Включить запись в «{_cut(tab)}»", "dlg_write_on")])
    kb_rows.append([_btn("🔁 Сверить ещё раз", "dlg_check")])
    if enabled:
        kb_rows.append([_btn("⛔ Выключить запись", "dlg_write_off")])
    kb_rows.append([_btn("📋 Другая вкладка", "dlg_sheet"), _to_screen()])
    return "\n".join(lines), _kb(kb_rows)


async def _show_check(callback, state: FSMContext, form: dict, tab: str) -> None:
    try:
        res = await _dry_run(form, tab)
    except Exception as exc:  # noqa: BLE001 — сбой API листа объясняем, не роняем экран
        logger.warning("delegations: сверка листа «%s» не удалась: %s", tab, type(exc).__name__)
        await state.update_data(dlg_tab=tab, dlg_dry=None)
        await _show(callback, _READ_FAILED.format(tab=_e(tab)),
                    _kb([[_btn("🔁 Сверить ещё раз", "dlg_check")],
                         [_btn("📋 Другая вкладка", "dlg_sheet"), _to_screen()]]))
        return
    dry = None if res is None else {"matched": int(res.get("matched") or 0),
                                    "new": int(res.get("new") or 0),
                                    "m_free": bool(res.get("m_free"))}
    await state.update_data(dlg_tab=tab, dlg_dry=dry)
    text, kb = _check_screen(res, tab, form)
    await _show(callback, text, kb)


@router.callback_query(F.data.startswith("dlg_tab:"))
async def dlg_tab(callback: types.CallbackQuery, state: FSMContext):
    idx = _tail_int(callback.data)
    tabs = (await state.get_data()).get("dlg_tabs") or []
    if idx is None or not 0 <= idx < len(tabs):
        await callback.answer(_STALE_TABS, show_alert=True)
        return
    form = await _current_form()
    if form is None:
        await render_screen(callback)
        await callback.answer()
        return
    await _show_check(callback, state, form, str(tabs[idx]))
    await callback.answer()


async def _picked_tab(form: dict, state: FSMContext) -> str | None:
    """Вкладка, которую менеджер смотрит сейчас: последняя выбранная в этой сессии, иначе —
    та, куда запись уже включена (после перезапуска FSM-данных нет)."""
    tab = (await state.get_data()).get("dlg_tab")
    if tab:
        return str(tab)
    return form.get("mirror_tab") if _form_enabled(form) else None


@router.callback_query(F.data == "dlg_check")
async def dlg_check(callback: types.CallbackQuery, state: FSMContext):
    form = await _current_form()
    if form is None:
        await render_screen(callback)
        await callback.answer()
        return
    tab = await _picked_tab(form, state)
    if not tab:
        await dlg_sheet(callback, state)
        return
    await _show_check(callback, state, form, tab)
    await callback.answer()


# ── включение и выключение записи ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "dlg_write_on")
async def dlg_write_on(callback: types.CallbackQuery, state: FSMContext):
    form = await _current_form()
    tab = await _picked_tab(form, state) if form else None
    if form is None or not tab:
        await callback.answer(_STALE_TAB, show_alert=True)
        return
    dry = (await state.get_data()).get("dlg_dry")
    if not dry:
        await _show_check(callback, state, form, tab)
        await callback.answer()
        return
    text = (
        f"Бот начнёт писать в лист «{_e(tab)}»:\n"
        f"• обновит {dry['matched']} строк (колонки D–M),\n"
        f"• добавит {dry['new']} новых строк после последней заполненной,\n"
        "• покрасит не-ЦА серым, зелёные строки и колонки P и правее не тронет.\n"
        "Включить?"
    )
    await _show(callback, text, _kb([[_btn("✅ Да, включить", "dlg_write_yes")],
                                     [_btn("❌ Отмена", "dlg_check")]]))
    await callback.answer()


@router.callback_query(F.data == "dlg_write_yes")
async def dlg_write_yes(callback: types.CallbackQuery, state: FSMContext):
    form = await _current_form()
    tab = await _picked_tab(form, state) if form else None
    if form is None or not tab:
        await callback.answer(_STALE_TAB, show_alert=True)
        return
    fid = int(form["id"])
    await ef.set_form_mirror(fid, tab, None)
    await ef.set_form_mirror_mode(fid, "yandex_export")
    n = await ef.requeue_form_answers(fid)
    logger.info("delegations: запись в лист включена (form=%s, rows=%s)", fid, n)
    await callback.answer(f"Запись включена, в очереди {n} строк")
    await render_screen(callback)


@router.callback_query(F.data == "dlg_write_off")
async def dlg_write_off(callback: types.CallbackQuery):
    form = await _current_form()
    if form is None or not _form_enabled(form):
        await callback.answer(_NOT_ENABLED, show_alert=True)
        return
    text = (f"Бот перестанет обновлять лист «{_e(form['mirror_tab'])}». "
            "Уже записанное останется. Выключить?")
    await _show(callback, text, _kb([[_btn("⛔ Да, выключить", "dlg_write_off_yes")],
                                     [_btn("❌ Отмена", "dlg_check")]]))
    await callback.answer()


@router.callback_query(F.data == "dlg_write_off_yes")
async def dlg_write_off_yes(callback: types.CallbackQuery):
    form = await _current_form()
    if form is None or not _form_enabled(form):
        await callback.answer(_NOT_ENABLED, show_alert=True)
        return
    fid = int(form["id"])
    await ef.set_form_mirror(fid, None, None)
    await ef.set_form_mirror_mode(fid, "bot")
    # Иначе экран показывал бы предупреждение о листе, который уже не выбран.
    await ef.set_form_mirror_warning(fid, None)
    logger.info("delegations: запись в лист выключена (form=%s)", fid)
    await callback.answer("Запись в лист выключена")
    await render_screen(callback)
