"""Phase 12 (FORUM-CHECKIN.md): раздел «✅ Отметки на форуме» — загрузка выгрузки офлайн-
приложения-сканера (D-09/D-10) и счётчик пришедших (D-17: статус «отмечен»/«пришёл», не
«зарегистрирован»). Сканер внутри Mini App (D-08) и ленивая выдача самого QR (D-01/D-03) —
отдельные задачи Phase 12, не эта (выдача QR уже сделана Квиком 260923 —
`services/checkin.py::build_checkin_qr`/`handlers/user_actions.py::show_my_checkin_qr`).

Форма шва — эталон `handlers/admin_purge.py`/`handlers/admin_cities.py`: своего `Router()`
нет, `from handlers.admin import router`, каждый декоратор — в одну строку со строковым
литералом (инвариант cap-теста `tests/test_roles_phase8.py`). Право — `checkin`
(`handlers/admin_caps.py` ADMIN_CAPS/`_ADMIN_MENU_ROWS`, `handlers/admin_sections.py`
SECTIONS «apps»).

T-12-01 (Tampering): найденный в файле QR-код НЕ доверенный ввод — строки отчёта «не найден»/
«не одобрен»/«прошлый сезон» показывают ФИО/город ИЗ САМОГО QR (см. FORUM-CHECKIN.md «Грубая
форма реализации»: они видны в строке, D-04), а не из БД — для «не найден» строки в БД
попросту нет. Это делает их таким же недоверенным текстом, как любой пользовательский ввод —
экранируем `html.escape` перед склейкой в HTML-сообщение (иначе фальшивый QR с «<a href=...>»
внутри ФИО отрисовался бы менеджеру как живая разметка)."""
import html
import logging

from aiogram import F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import cities_module_on, city_label, city_scope, enabled_cities
from database.db import (
    count_approved_current_season,
    count_checkins_by_point,
    get_user,
    record_checkin,
    reissue_checkin_token,
)
from handlers.admin import router
from handlers.admin_core import _admin_city_scope
from handlers.states import CheckinImport
from keyboards.builders import get_cancel_kb
from services.checkin import (
    DENIAL_REASON_TEXT,
    ENTRY_POINT,
    ENTRY_POINT_LABEL,
    build_checkin_qr,
    checkin_denial,
    current_event_tag,
    decode_scan_export,
    find_checkin_records,
    mark_arrived_in_sheet,
    parse_qr_payload,
    resolve_scanned_user,
)

logger = logging.getLogger(__name__)

# T-12-02 (DoS): тот же потолок, что у «📥 Импорт прошлого события» (admin_cities.py) — гейт
# ДО bot.download, не после.
_IMPORT_MAX_BYTES = 20 * 1024 * 1024
_REPORT_ROW_LIMIT = 20

# `services.checkin.checkin_denial` возвращает машинный код допуска D-02 ("no_user" |
# "not_approved" | "past_season") -- строка отчёта нужна человеческая, эта таблица -- единая
# точка перевода кода в текст, чтобы не завести два места, знающих коды денайла.
_DENIAL_LABELS = {
    "no_user": "не найден",
    "not_approved": "не одобрен",
    "past_season": "прошлый сезон",
    # Форум-ночь B1 (идея №10): QR перевыпущен (database.db.checkin_token_replacements) —
    # компактная версия DENIAL_REASON_TEXT["token_replaced"] для строки отчёта загрузки.
    "token_replaced": "QR заменён",
}


async def _one_city_line(label: str, city_sc) -> tuple[str, int, int] | None:
    """Строка «<Город>: пришли N из M» + сырые числа для итога. `None`, если в городе нет ни
    одного одобренного текущего сезона (задача A2 просила показывать только города, где есть
    одобренные -- пустой регион 30.10 не должен маячить строкой «0 из 0» рядом с 03.10)."""
    approved = await count_approved_current_season(city_scope=city_sc)
    if approved == 0:
        return None
    arrived = await count_checkins_by_point(ENTRY_POINT, city_scope=city_sc)
    return f"{label}: пришли {arrived} из {approved}", arrived, approved


async def _counter_line(admin_id: int) -> str:
    """«Пришли: N из M одобренных» (задача A2, FORUM-CHECKIN.md) — считает по точке «Вход».

    03.10 форумы СПб и Тюмени идут ОДНОВРЕМЕННО с ещё открытым набором в Москве -- один общий
    счётчик на всё событие путает «пришедших в регионе» с «ещё набирающимися в Москве».
    Три ветки:
    1. Менеджер закреплён за одним городом (`_admin_city_scope` -- тот же резолвер, что у
       очереди заявок) -- показываем ТОЛЬКО его город, без построчного списка остальных.
    2. Модуль городов выключен -- старое нескопированное поведение байт-в-байт (city_scope=None
       и на счётчике, и на знаменателе).
    3. Менеджер видит «Все города» -- построчно по каждому включённому городу, где есть хотя бы
       один одобренный текущего сезона, плюс «Итого» под списком."""
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        approved = await count_approved_current_season(city_scope=own_scope)
        arrived = await count_checkins_by_point(ENTRY_POINT, city_scope=own_scope)
        return f"Пришли: {arrived} из {approved} одобренных"

    if not await cities_module_on():
        approved = await count_approved_current_season()
        arrived = await count_checkins_by_point(ENTRY_POINT)
        return f"Пришли: {arrived} из {approved} одобренных"

    lines: list[str] = []
    total_arrived = 0
    total_approved = 0
    for c in await enabled_cities():
        code = c["code"]
        result = await _one_city_line(await city_label(code), city_scope(code))
        if result is None:
            continue
        line, arrived, approved = result
        lines.append(line)
        total_arrived += arrived
        total_approved += approved

    if not lines:
        return "Пришли: 0 из 0 одобренных"
    lines.append(f"Итого: {total_arrived} из {total_approved}")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_checkin")
async def show_admin_checkin(callback: types.CallbackQuery):
    text = (
        "✅ <b>Отметки на форуме</b>\n\n"
        f"{await _counter_line(callback.from_user.id)}\n\n"
        "Выгрузите историю сканов из приложения-сканера в CSV и пришлите сюда файлом — "
        "отмечу всех, кого найду."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Загрузить файл сканера", callback_data="checkin_upload_start")],
    ])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "checkin_upload_start")
async def checkin_upload_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})
    await callback.message.answer(
        "Выгрузите историю из приложения-сканера в CSV и пришлите сюда файлом.\n\n"
        "Размер — до 20 МБ.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CheckinImport.waiting_file)
    await callback.answer()


@router.message(StateFilter(CheckinImport), Command("cancel"))
@router.message(StateFilter(CheckinImport), F.text == "Отмена")
async def cancel_checkin_import(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Ничего не отмечено.", reply_markup=ReplyKeyboardRemove())


@router.message(CheckinImport.waiting_file, F.document)
async def checkin_import_file_step(message: types.Message, state: FSMContext, bot: Bot):
    if (message.document.file_size or 0) > _IMPORT_MAX_BYTES:
        await message.answer("Файл больше 20 МБ — столько я принять не могу.")
        return

    buf = await bot.download(message.document.file_id)
    buf.seek(0)
    text = decode_scan_export(buf.read())

    tag = await current_event_tag()
    records = find_checkin_records(text, tag)
    if not records:
        await message.answer(
            "В файле не нашёл ни одного QR форума — проверьте, что выгружали историю сканов "
            "именно этого форума.",
            reply_markup=get_cancel_kb(),
        )
        return  # остаёмся в waiting_file -- можно сразу прислать другой файл

    await state.update_data(checkin_records=records)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ENTRY_POINT_LABEL, callback_data=f"checkin_point:{ENTRY_POINT}")],
    ])
    await message.answer(f"Нашёл кодов: {len(records)}.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Отметить точкой:", reply_markup=kb)


@router.message(CheckinImport.waiting_file)
async def checkin_import_file_invalid(message: types.Message):
    await message.answer("Пришли файл выгрузки документом (не фото и не архив).")


@router.callback_query(StateFilter(CheckinImport), F.data.startswith("checkin_point:"))
async def checkin_point_pick(callback: types.CallbackQuery, state: FSMContext):
    point = callback.data.split(":", 1)[1]
    data = await state.get_data()
    records = data.get("checkin_records") or []
    await state.set_state(None)

    new_n = 0
    dup_n = 0
    # (reason, row) -- reason — человеческая причина из _DENIAL_LABELS, а не жёстко
    # закодированные категории: денайл-правило (services.checkin.checkin_denial) само решает
    # допуск, отчёт только переводит код в текст (не дублирует логику допуска D-02).
    flagged: list[tuple[str, dict]] = []

    for rec in records:
        parsed = parse_qr_payload(rec["qr"])
        token = parsed["token"]
        user, denial_code = await resolve_scanned_user(token)
        if denial_code is not None:
            flagged.append((_DENIAL_LABELS.get(denial_code, denial_code), parsed))
            continue
        approx = rec["scanned_at"] is None
        status, ts = await record_checkin(
            user["telegram_id"], point, source="csv",
            scanned_at=rec["scanned_at"], approx=approx,
            by_staff_id=callback.from_user.id,
        )
        await mark_arrived_in_sheet(user["telegram_id"], status, ts)
        if status == "new":
            new_n += 1
        else:
            dup_n += 1

    not_found_n = sum(1 for reason, _row in flagged if reason == _DENIAL_LABELS["no_user"])
    replaced_n = sum(1 for reason, _row in flagged if reason == _DENIAL_LABELS["token_replaced"])
    not_approved_n = len(flagged) - not_found_n - replaced_n

    lines = [
        "✅ <b>Отметки загружены</b>",
        "",
        f"Отмечено новых: {new_n} · уже были: {dup_n} · "
        f"не найдено: {not_found_n} · не одобрены: {not_approved_n} · "
        f"QR заменён: {replaced_n}",
    ]
    shown = flagged[:_REPORT_ROW_LIMIT]
    if shown:
        lines.append("")
        lines.append("<b>Требуют внимания:</b>")
        for reason, row in shown:
            name = html.escape(row["full_name"] or "(без имени)")
            city = html.escape(row["city"] or "—")
            lines.append(f"❔ {name} · {city} — {reason}")
    remaining = len(flagged) - len(shown)
    if remaining > 0:
        lines.append(f"…и ещё {remaining}")
    lines.append("")
    lines.append(await _counter_line(callback.from_user.id))

    from handlers.admin_sections import op_return_keyboard  # ленивый шов (см. docstring модуля)
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=await op_return_keyboard(callback.from_user.id, "admin_checkin"),
    )
    await callback.answer()


# ── Форум-ночь B1 (идея №10): перевыпуск QR ─────────────────────────────────────────────────
# Кнопка — на карточке `/find` (handlers/admin.py::cmd_find_user), сама операция и подтверждение
# живут здесь: тот же шов, что остальной чек-ин-функционал. Право то же, что у карточки-
# источника («moderate_reg» — управление делегатским аккаунтом, не рутинное сканирование на
# входе, поэтому не капа «checkin»).

@router.callback_query(F.data.startswith("checkin_reissue:"))
async def checkin_reissue_confirm(callback: types.CallbackQuery):
    tid = int(callback.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Да, перевыпустить", callback_data=f"checkin_reissue_yes:{tid}"),
        InlineKeyboardButton(text="Отмена", callback_data="checkin_reissue_no"),
    ]])
    await callback.message.answer(
        "⚠️ Старый QR перестанет работать — если делегат уже сохранил скриншот, тот больше не "
        "пропустит на форум. Перевыпустить?",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("checkin_reissue_yes:"))
async def checkin_reissue_go(callback: types.CallbackQuery):
    tid = int(callback.data.split(":", 1)[1])
    new_token = await reissue_checkin_token(tid)
    if new_token is None:
        await callback.message.edit_text("Не нашёл делегата — возможно, уже удалён.")
        await callback.answer()
        return

    user = await get_user(tid)
    denial_code = await checkin_denial(user)
    if denial_code is not None:
        note = (
            "Новый QR готов, но у делегата сейчас нет пропуска "
            f"({DENIAL_REASON_TEXT.get(denial_code, denial_code)}) — не отправлен."
        )
    else:
        try:
            png, caption = await build_checkin_qr(user)
            await callback.bot.send_photo(
                tid, BufferedInputFile(png, filename="checkin_qr.png"), caption=caption,
            )
            note = "Новый QR отправлен делегату."
        except Exception:
            logger.warning(
                "checkin_reissue_go: не удалось отправить новый QR делегату tid=%s", tid, exc_info=True,
            )
            note = (
                "Новый QR готов, но отправить делегату не получилось (заблокировал бота?) — "
                "попросите открыть «🎟 Мой QR» самому."
            )

    await callback.message.edit_text(f"✅ QR перевыпущен.\n{note}")
    await callback.answer()


@router.callback_query(F.data == "checkin_reissue_no")
async def checkin_reissue_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено. QR не менялся.")
    await callback.answer()
