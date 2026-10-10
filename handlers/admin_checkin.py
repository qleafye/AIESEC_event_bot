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
from datetime import datetime

from aiogram import F, types, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import (
    cities_module_on,
    city_label,
    city_label_or_none,
    city_scope,
    default_city_code,
    enabled_cities,
    get_setting_typed_for_city,
    normalize_city,
    per_city_key,
)
from config import config
from database.db import (
    checkin_qr_send_counts,
    count_approved_current_season,
    count_checkins_by_point,
    get_program_session,
    get_staff_city,
    get_user,
    reissue_checkin_token,
)
from handlers.admin import router
from handlers.admin_core import _admin_city_scope
from handlers.states import CheckinImport, CheckinQrTimeEdit, CheckinTestUpload
from keyboards.builders import get_cancel_kb
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed
from settings_validation import validate_setting_value
from services.checkin import (
    DENIAL_REASON_TEXT,
    ENTRY_POINT,
    ENTRY_POINT_LABEL,
    build_checkin_qr,
    build_test_qr,
    checkin_denial,
    current_event_tag,
    decode_scan_export,
    find_checkin_records,
    parse_qr_payload,
    record_arrival,
    resolve_scanned_user,
)
from services.checkin_broadcast import (
    _times_for,
    broadcast_enabled_for,
    manual_send_block_reason,
    qr_send_report,
    morning_run_at,
    pending_broadcast_count,
    schedule_city_jobs,
    send_broadcast,
)
from services.checkin_not_arrived import (
    pending_count as checkin_not_arrived_pending_count,
    report_text as checkin_not_arrived_report_text,
    send as checkin_not_arrived_send,
    summary_text as checkin_not_arrived_summary_text,
)
from services.program import checkin_session_points, scanned_outside_session_window
from services.reject_rules import forum_date_for
from services.timeutil import city_offset_hours, msk_now, shift_hours
from services import checkin_arrival
from services import checkin_csv_import as _csv_import


async def _city_now(city: str | None):
    """Местное «сейчас» города (МСК + «🕐 Часовой пояс»); `msk_now` берётся из этого модуля."""
    return shift_hours(msk_now(), await city_offset_hours(city))


logger = logging.getLogger(__name__)

# Форум-ночь п.3 (D-03, идея №2): сентинел «нет конкретного города» в callback_data (модуль
# городов выключен, или экран уже нацелен на единственную область без города) — city_codes()
# закрытое множество коротких кодов, но None в callback_data не положишь, поэтому отдельная
# строка-плейсхолдер, не пересекающаяся ни с одним настоящим кодом.
_NO_CITY = "_all"


def _encode_city(code: str | None) -> str:
    return code or _NO_CITY


def _decode_city(raw: str) -> str | None:
    return None if raw == _NO_CITY else raw

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
    "unknown_pass_kind": "неизвестный тип пропуска",
}


async def _one_city_line(label: str, city_sc, day: str | None = None) -> tuple[str, int, int] | None:
    """Строка «<Город>: пришли N из M» + числа для итога. `None` — в городе нет одобренных
    текущего сезона (пустой регион не маячит строкой «0 из 0»)."""
    arrived, approved = await checkin_arrival.arrived_counts(city_sc, day)
    if approved == 0:
        return None
    return f"{label}: пришли {arrived} из {approved}", arrived, approved


async def _screen_scope(admin_id: int, city: str | None):
    """Город экрана: открыт из хаба города (`city` в кнопке) — он, даже при шапке «Все города»;
    иначе — привязка менеджера (`_admin_city_scope`)."""
    if city is not None and await cities_module_on():
        return city_scope(city)
    return await _admin_city_scope(admin_id)


async def _forum_today(code: str | None) -> bool:
    from services.forum_days import forum_window
    window = await forum_window(code)
    return window is not None and window[0] <= (await _city_now(code)).date() <= window[1]


async def _counter_line(admin_id: int, city: str | None = None) -> str:
    """«Пришли N из M одобренных» (задача A2, FORUM-CHECKIN.md) — тот же запрос, что статистика
    прихода (`services.checkin_arrival.arrived_counts`: одобренные текущего сезона со входом).
    Вход каждый день: в день форума (сегодня уже был хоть один вход) — «Сегодня пришли» по
    входу сегодняшнего дня, иначе — «Пришли за форум» (хоть один вход).

    Три ветки: город экрана -- только он; модуль городов выключен -- без фильтра; «Все города»
    -- построчно по городам с одобренными (в день форума -- только с форумом сегодня), «Итого»."""
    day = await checkin_arrival.counter_day()
    head = "Сегодня пришли" if day else "Пришли за форум"
    own_scope = await _screen_scope(admin_id, city)
    if own_scope is not None or not await cities_module_on():
        arrived, approved = await checkin_arrival.arrived_counts(own_scope, day)
        return f"{head}: {arrived} из {approved} одобренных"

    lines: list[str] = []
    total_arrived = 0
    total_approved = 0
    today_codes = await checkin_arrival.today_forum_codes(day)  # «Сегодня» — только города с форумом
    for c in await enabled_cities():
        code = c["code"]
        if today_codes and code not in today_codes:
            continue
        result = await _one_city_line(await city_label(code), city_scope(code), day)
        if result is None:
            continue
        line, arrived, approved = result
        lines.append(line)
        total_arrived += arrived
        total_approved += approved

    if not lines:
        return f"{head}: 0 из 0 одобренных"
    lines.insert(0, "Сегодня:" if day else "За форум:")
    if len(lines) > 2:  # один город — его строка и есть итог
        lines.append(f"Итого: {total_arrived} из {total_approved}")
    return "\n".join(lines)


# Почему автоматическая рассылка QR города не стоит — ключи те же, что `reason` у
# `services.checkin_broadcast.schedule_city_jobs` (маппинг по строке: незнакомый ключ — без
# подписи, строка счётчика остаётся как есть).
_QR_NOT_SCHEDULED_LABELS = {
    "no_date": "дата форума не задана — рассылка не поставлена",
    "disabled": "автоматическая рассылка выключена (включить — «⚙️ Настройки QR»)",
    "bad_date": "дата форума записана с ошибкой — рассылка не поставлена, поправьте дату форума",
    "past": "форум уже прошёл — рассылка не поставлена",
}


async def _qr_not_scheduled_reason(code: str | None) -> str | None:
    """Та же развилка, что у `schedule_city_jobs`, но без побочных эффектов (экран только
    читает, джобы не трогает) + «past»: дата форума раньше сегодняшней по Москве."""
    date_str = await forum_date_for(code)
    if date_str is None:
        return "no_date"
    if not await broadcast_enabled_for(code):
        return "disabled"
    try:
        day = datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
    except (TypeError, ValueError, AttributeError):
        return "bad_date"
    if day < (await _city_now(code)).date():
        return "past"
    return None


async def _qr_status_line(label: str | None, code: str | None) -> str:
    got, confirmed = await checkin_qr_send_counts(city_scope=city_scope(code))
    prefix = f"{label}: " if label else ""
    line = f"{prefix}QR получили {got} · подтвердили {confirmed}"
    reason = await _qr_not_scheduled_reason(code)
    reason_text = _QR_NOT_SCHEDULED_LABELS.get(reason or "")
    if reason_text:
        line += f" · {reason_text}"
    elif reason is None and await _morning_repeat_passed_today(code):
        line += " · утренний повтор сегодня уже прошёл"
    return line


async def _morning_repeat_passed_today(code: str | None) -> bool:
    """День форума, а время утреннего повтора (тот же `morning_run_at`, что у джобы) уже
    позади — например, QR включили днём: менеджер должен знать, что повтора сегодня не будет."""
    date_str = await forum_date_for(code)
    _evening, morning = await _times_for(code)
    morn_at = morning_run_at(date_str, morning)
    now = await _city_now(code)  # время рассылки — по часам города
    return morn_at is not None and morn_at.date() == now.date() and morn_at <= now


async def _qr_broadcast_section(admin_id: int, city: str | None = None) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Форум-ночь п.3 (D-03, идея №2): блок «🎟 Рассылка QR» экрана «✅ Отметки на форуме» —
    строка(и) «QR получили N · подтвердили M» + кнопки «📤 Разослать сейчас»/«⚙️ Настройки QR».
    Мастер-тумблер `checkin_qr_enabled` выключен -> блока нет вовсе (пустая строка, без кнопок)
    — тот же приём, что «показываем только то, что реально работает».

    Три ветки — байт-в-байт та же развилка, что у `_counter_line` выше (закреплённый город /
    модуль выключен / «Все города» построчно по каждому включённому)."""
    if await get_setting_typed("checkin_qr_enabled") != "on":
        return "", []

    own_scope = await _screen_scope(admin_id, city)
    if own_scope is not None:
        code = own_scope[0]
        line = await _qr_status_line(None, code)
        buttons = [
            [InlineKeyboardButton(text="📤 Разослать QR сейчас", callback_data=f"checkinqr_send:{_encode_city(code)}")],
            [InlineKeyboardButton(text="⚙️ Настройки QR", callback_data=f"checkinqr_cfg:{_encode_city(code)}")],
        ]
        return line, buttons

    if not await cities_module_on():
        line = await _qr_status_line(None, None)
        buttons = [
            [InlineKeyboardButton(text="📤 Разослать QR сейчас", callback_data=f"checkinqr_send:{_NO_CITY}")],
            [InlineKeyboardButton(text="⚙️ Настройки QR", callback_data=f"checkinqr_cfg:{_NO_CITY}")],
        ]
        return line, buttons

    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []
    for c in await enabled_cities():
        code = c["code"]
        label = await city_label(code)
        lines.append(await _qr_status_line(label, code))
        # Подписи словами + город, по кнопке в ряд — две иконки с названием города в одной
        # строке менеджер не расшифрует.
        buttons.append([InlineKeyboardButton(
            text=f"📤 Разослать QR сейчас — {label}", callback_data=f"checkinqr_send:{_encode_city(code)}",
        )])
        buttons.append([InlineKeyboardButton(
            text=f"⚙️ Настройки QR — {label}", callback_data=f"checkinqr_cfg:{_encode_city(code)}",
        )])
    return "\n".join(lines), buttons


# ── Форум-ночь п.6 (D-25, идея №14): шаблон «Не пришёл» ─────────────────────────────────────

async def _not_arrived_status_line(label: str | None, code: str | None) -> str:
    prefix = f"{label}: " if label else ""
    text = await checkin_not_arrived_summary_text(city_scope=city_scope(code))
    return f"{prefix}{text}"


async def _not_arrived_section(admin_id: int, city: str | None = None) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Блок «🚪 Не пришли» — строка(и) сводки за сегодня + кнопка(и) «📨 Написать не пришедшим».
    Три ветки — та же развилка, что у `_qr_broadcast_section`/`_counter_line` выше.

    «Все города»: строку (и кнопку) города показываем, только если в нём есть хоть один
    одобренный текущего сезона — тот же довод и приём, что у `_one_city_line` в `_counter_line`
    (пустой регион не должен маячить нулями рядом с городом, где форум уже идёт)."""
    own_scope = await _screen_scope(admin_id, city)
    if own_scope is not None:
        code = own_scope[0]
        if not await _forum_today(code):  # кнопка, которая всегда отвечает «некому», путает
            return "Сегодня у города нет форума — «не пришли» считаются в дни форума.", []
        line = await _not_arrived_status_line(None, code)
        buttons = [[InlineKeyboardButton(
            text="📨 Написать не пришедшим", callback_data=f"cna_send:{_encode_city(code)}",
        )]]
        return line, buttons

    if not await cities_module_on():
        line = await _not_arrived_status_line(None, None)
        buttons = [[InlineKeyboardButton(
            text="📨 Написать не пришедшим", callback_data=f"cna_send:{_NO_CITY}",
        )]]
        return line, buttons

    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []
    for c in await enabled_cities():
        code = c["code"]
        city_sc = city_scope(code)
        if not await _forum_today(code) or await count_approved_current_season(city_scope=city_sc) == 0:
            continue  # «не пришли» есть только у городов, где сегодня идёт форум
        label = await city_label(code)
        lines.append(await _not_arrived_status_line(label, code))
        buttons.append([InlineKeyboardButton(
            text=f"📨 Написать не пришедшим — {label}", callback_data=f"cna_send:{_encode_city(code)}",
        )])
    return "\n".join(lines) or "Сегодня ни в одном городе нет форума.", buttons


async def render_admin_checkin(admin_id: int, city: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «✅ Отметки на форуме». Последняя строка — «◀️ Назад» в раздел; хаб «🎪 Форум:
    функции» подменяет её своей (handlers/admin_forum_hub_nav.py)."""
    from handlers.admin_sections import back_button  # ленивый шов (см. docstring модуля)
    qr_line, qr_buttons = await _qr_broadcast_section(admin_id, city)
    qr_block = f"\n\n🎟 <b>Рассылка QR</b>\n{qr_line}" if qr_line else ""
    not_arrived_line, not_arrived_buttons = await _not_arrived_section(admin_id, city)
    not_arrived_block = f"\n\n🚪 <b>Не пришли</b> (сегодня)\n{not_arrived_line}"
    text = (
        "✅ <b>Отметки на форуме</b>\n\n"
        f"{await _counter_line(admin_id, city)}"
        f"{qr_block}"
        f"{not_arrived_block}\n\n"
        "Выгрузите историю сканов из приложения-сканера в CSV и пришлите сюда файлом — "
        "отмечу всех, кого найду."
    )
    # Рассылка QR, «Написать не пришедшим», сводка прихода — менеджерские (moderate_reg):
    # волонтёру с одним правом `checkin` эти кнопки не рисуем, иначе тап отвечает «Недостаточно прав».
    from handlers.admin_caps import _holds, resolve_capabilities
    is_manager = _holds(await resolve_capabilities(admin_id), "moderate_reg")
    if not is_manager:
        qr_buttons, not_arrived_buttons = [], []
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *qr_buttons,
        *not_arrived_buttons,
        [InlineKeyboardButton(text="📤 Загрузить файл сканера", callback_data="checkin_upload_start")],
        # Форум-ночь B4 (идея №8): пробная выгрузка — ничего не отмечает, только проверяет
        # формат/читаемость приложения волонтёра.
        [InlineKeyboardButton(text="🧪 Проверить приложение-сканер", callback_data="checkin_test_start")],
        [InlineKeyboardButton(text="🧪 Учебные QR", callback_data="checkin_training_sheet")],
        *await _venue_entry_rows(admin_id),
        [back_button("admin_checkin", "◀️ Назад")],
    ])
    if is_manager:  # бэклог п.10: сводка прихода
        kb.inline_keyboard.insert(0, [
            InlineKeyboardButton(text="📊 Статистика прихода", callback_data="checkin_stats"),
            InlineKeyboardButton(text="📍 Сейчас на площадке", callback_data="checkin_floor"),
        ])
    return text, kb


@router.callback_query(F.data == "admin_checkin")
async def show_admin_checkin(callback: types.CallbackQuery):
    text, kb = await render_admin_checkin(callback.from_user.id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cna_send:"))
async def cna_send_confirm(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    n = await checkin_not_arrived_pending_count(city_scope=city_scope(code))
    if n == 0:
        await callback.answer(
            "Отправлять некому: все одобренные отмечены на входе или уже получили вопрос "
            "сегодня, либо у города сегодня нет форума (по его дате).", show_alert=True,
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, отправить", callback_data=f"cna_send_go:{_encode_city(code)}:{n}"),
        InlineKeyboardButton(text="Отмена", callback_data="cna_send_no"),
    ]])
    where = f" города {html.escape(await city_label(code))}" if code and await cities_module_on() else ""
    whom = "делегату" if n % 10 == 1 and n % 100 != 11 else "делегатам"
    # Текст — тот же, что уйдёт (services/checkin_not_arrived.send), чтобы менеджер видел, что шлёт.
    body = await get_setting_typed_for_city("checkin_not_arrived_text", code) or ""

    def _confirm(preview: str) -> str:
        return (
            f"Уйдёт {n} {whom}{where} (не пришли сегодня — не отмечены на входе). "
            "Если отметки ещё загружаются файлами "
            "(CSV-режим) — часть пришедших получит сообщение по ошибке.\n\n"
            f"<b>Текст сообщения:</b>\n<blockquote>{preview}</blockquote>\n"
            "Под ним кнопки: «🚶 Уже еду», «😔 Не смогу прийти», «📍 Я на месте».\n"
            "Текст правится в «⚙️ Настройки» → «📋 Заявки» → «🚪 «Не пришёл»: текст рассылки».\n\n"
            "Отправить?"
        )
    try:  # текст поддерживает HTML — показываем так, как его увидит делегат
        await callback.message.answer(_confirm(body), parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(_confirm(html.escape(body)), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cna_send_go:"))
async def cna_send_go(callback: types.CallbackQuery):
    # «cna_send_go:<город>:<N с экрана подтверждения>»; старые кнопки — без N.
    raw, _sep, n_raw = callback.data.split(":", 1)[1].partition(":")
    code, expected = _decode_city(raw), int(n_raw) if n_raw.isdigit() else None
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    # T-12-03 idiom (тот же приём, что checkinqr_send_go выше): отвечаем на callback СРАЗУ,
    # рассылка может занять время, и убираем клавиатуру ДО вызова — повторный тап уже
    # физически не по чему нажимать.
    await callback.answer("Отправляю…")
    await callback.message.edit_text("⏳ Отправляю «Не пришёл»...", reply_markup=None)
    result = await checkin_not_arrived_send(city=code, city_scope=city_scope(code))
    await callback.message.answer(checkin_not_arrived_report_text(result, expected))


@router.callback_query(F.data == "cna_send_no")
async def cna_send_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено. Ничего не отправлено.")
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
    training_n = _training.count_training_codes(text)  # бэклог №7: учебные коды не отмечаем
    records = _training.drop_training_records(find_checkin_records(text, tag))
    if not records and training_n:
        await state.set_state(None)
        await message.answer(_training.training_only_text(training_n), reply_markup=ReplyKeyboardRemove())
        return
    if not records:
        await message.answer(
            "В файле не нашёл ни одного QR форума — проверьте, что выгружали историю сканов "
            "именно этого форума.",
            reply_markup=get_cancel_kb(),
        )
        return  # остаёмся в waiting_file -- можно сразу прислать другой файл

    await state.update_data(checkin_records=records, checkin_training_n=training_n)
    await message.answer(f"Нашёл кодов: {len(records)}.", reply_markup=ReplyKeyboardRemove())
    city = await _resolve_checkin_screen_city(message.from_user.id)
    if city is None:
        await message.answer("Из какого города точка?", reply_markup=await _city_picker_kb())
    else:
        await message.answer("Отметить точкой:", reply_markup=await _point_picker_kb(city))


@router.message(CheckinImport.waiting_file)
async def checkin_import_file_invalid(message: types.Message):
    await message.answer("Пришли файл выгрузки документом (не фото и не архив).")


# Форум-ночь п.5 (D-18): точки отметки — «Вход» + сессии СЕГОДНЯ выбранного города, «идёт
# сейчас» — первыми (`services.program.checkin_session_points`). Город — тот же трёхветочный
# приём, что `handlers/admin_program.py::_resolve_city_for_screen` (закреплённый город /
# модуль выключен -> единственный дефолтный город / иначе -> экран выбора).

async def _resolve_checkin_screen_city(admin_id: int) -> str | None:
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        return own_scope[0]
    if not await cities_module_on():
        return default_city_code()
    return None


async def _volunteer_bound_city(admin_id: int) -> str | None:
    """D-26 (24.09): настоящая ПРИВЯЗКА волонтёра к городу (`database.db.get_staff_city`), а
    НЕ выбор фильтра в панели (`_resolve_checkin_screen_city`/`_admin_city_scope` отдают город и
    непривязанному менеджеру при выключенном модуле — дефолтный, это не то же самое). Суперадмин
    (`config.ADMIN_IDS`) не привязан никогда, модуль городов выключен -> тоже без привязки. Тот
    же приём, что `miniapp/routers/checkin.py::_bound_city` — бот и Mini App одинаково решают,
    кого ограничивать, на входе и в отчёте загрузки CSV."""
    if admin_id in config.ADMIN_IDS:
        return None
    if not await cities_module_on():
        return None
    bound = await get_staff_city(admin_id)
    return normalize_city(bound) if bound else None


async def _point_picker_kb(city: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=ENTRY_POINT_LABEL, callback_data=f"checkin_point:{ENTRY_POINT}")],
    ]
    for sp in await checkin_session_points(city):
        label = ("🔴 " if sp["live"] else "") + sp["label"]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"checkin_point:{sp['point']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _city_picker_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"checkin_point_city:{c['code']}")]
        for c in await enabled_cities()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Без StateFilter: после перезапуска бота состояние (и записи файла в нём) пропадают — кнопка
# выбора точки должна ответить «пришлите файл заново», а не висеть со спиннером. Записи
# снимаются с состояния перед отметкой — повторный тап по той же кнопке попадает сюда же.
async def _import_records_or_explain(callback: types.CallbackQuery, state: FSMContext) -> list[dict] | None:
    data = await state.get_data()
    if data.get("checkin_importing"):  # двойной тап по точке: файл уже отмечается
        await callback.answer("Уже отмечаю этот файл — дождитесь отчёта.")
        return None
    records = data.get("checkin_records") or []
    if not records:
        await callback.answer()
        await callback.message.answer(_csv_import.LOST_FILE_TEXT)
        return None
    return records


@router.callback_query(F.data.startswith("checkin_point_city:"))
async def checkin_point_city_pick(callback: types.CallbackQuery, state: FSMContext):
    if await _import_records_or_explain(callback, state) is None:
        return
    code = callback.data.split(":", 1)[1]
    await callback.message.answer("Отметить точкой:", reply_markup=await _point_picker_kb(code))
    await callback.answer()


@router.callback_query(F.data.startswith("checkin_point:"))
async def checkin_point_pick(callback: types.CallbackQuery, state: FSMContext):
    records = await _import_records_or_explain(callback, state)
    if records is None:
        return
    point = callback.data.split(":", 1)[1]
    data = await state.get_data()
    # Записи снимаются с состояния ДО первого обращения к базе: второй, параллельный колбэк
    # двойного тапа их уже не найдёт и не импортирует файл второй раз.
    await state.update_data(checkin_records=None, checkin_importing=True)
    try:
        await _checkin_point_import(callback, state, records, point, data)
    finally:
        await state.update_data(checkin_importing=None)


async def _checkin_point_import(callback, state: FSMContext, records: list[dict], point: str, data: dict):

    # Точка выбрана ОДНА на весь загруженный файл — сессию (если это точка сессии) и её отчётный
    # интервал времени (D-18..D-20) достаточно достать один раз, а не на каждую строку.
    session = await get_program_session(int(point.split(":", 1)[1])) if point.startswith("session:") else None
    if point.startswith("session:") and session is None:
        # Сессию удалили/пересоздали, пока выбирали точку: ничего не отмечаем, файл в состоянии
        # остаётся — сразу новый выбор точки.
        await state.update_data(checkin_records=records)
        await callback.answer()
        city = (await _resolve_checkin_screen_city(callback.from_user.id)) or default_city_code()
        await callback.message.answer(_csv_import.SESSION_GONE_TEXT, reply_markup=await _point_picker_kb(city))
        return
    await state.set_state(None)
    # Ответ на кнопку сразу: файл на сотни строк отмечается дольше 15 секунд, поздний ответ
    # Telegram уже не принимает.
    await callback.answer("Отмечаю…")
    if len(records) > _csv_import.PROGRESS_THRESHOLD:
        await callback.message.answer(_csv_import.progress_text(len(records)))

    # D-26 (24.09): волонтёр/менеджер, ПРИВЯЗАННЫЙ к городу, загружает выгрузку сканера только
    # своей стойки — делегаты чужого города НЕ отмечаются (отдельная строка отчёта). Только для
    # «Входа» — сессии и так ограничены своим городом выбором точки и проверкой в
    # `record_arrival` (`wrong_city`).
    bound_city = await _volunteer_bound_city(callback.from_user.id) if point == ENTRY_POINT else None
    res = await _csv_import.import_records(
        records, point, session=session, bound_city=bound_city,
        staff_id=callback.from_user.id, bot=callback.bot, labels=_DENIAL_LABELS,
    )
    await _venue_log.log_by(  # идея №31: загрузка файла — одна строка журнала площадки
        callback.from_user, _venue_log.ACTION_CSV_UPLOAD, source="csv", point=point,
        city=(session or {}).get("city") or bound_city or await _resolve_checkin_screen_city(callback.from_user.id),
        details={key: res[key] for key in ("new", "duplicate", "moved", "not_found", "not_approved")},
    )

    lines = await _csv_import.report_lines(res, row_limit=_REPORT_ROW_LIMIT)
    if data.get("checkin_training_n"):
        lines.insert(3, _training.training_report_line(data["checkin_training_n"]))
    lines.append("")
    lines.append(await _counter_line(callback.from_user.id))

    from handlers.admin_sections import op_return_keyboard  # ленивый шов (см. docstring модуля)
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=await op_return_keyboard(callback.from_user.id, "admin_checkin"),
    )


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

    await _venue_log.log_by(callback.from_user, _venue_log.ACTION_REISSUE_QR, telegram_id=tid)
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


# ── Форум-ночь B4 (идея №8): пробная выгрузка приложения-сканера ────────────────────────────
# Волонтёр проверяет СВОЁ приложение-сканер заранее (не в последнюю ночь перед форумом): сканит
# любой настоящий QR ЭТОГО события (свой «🎟 Мой QR», делегата, или фиктивный «🧪 Показать
# тестовый QR» ниже — если сам не делегат) и присылает выгрузку сюда. Бот НИЧЕГО не отмечает
# (никакого record_checkin) — только парсит той же логикой (find_checkin_records), что реальная
# загрузка, и отвечает, читается ли формат и время скана.

_TEST_UPLOAD_PROMPT = (
    "🧪 Отсканируйте любой настоящий QR ЭТОГО форума вашим приложением-сканером — свой «🎟 Мой "
    "QR» (если вы делегат) или тестовый код ниже — и пришлите сюда выгрузку файлом.\n\n"
    "Ничего не отмечу — только проверю, подходит ли формат."
)


@router.callback_query(F.data == "checkin_test_start")
async def checkin_test_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Показать тестовый QR", callback_data="checkin_test_qr")],
    ])
    await callback.message.answer(_TEST_UPLOAD_PROMPT, reply_markup=kb)
    await callback.message.answer("Пришлите файл выгрузки или отмените:", reply_markup=get_cancel_kb())
    await state.set_state(CheckinTestUpload.waiting_file)
    await callback.answer()


@router.callback_query(F.data == "checkin_test_qr")
async def checkin_test_qr(callback: types.CallbackQuery):
    png, caption = await build_test_qr()
    await callback.bot.send_photo(
        callback.from_user.id, BufferedInputFile(png, filename="test_qr.png"), caption=caption,
    )
    await callback.answer()


@router.message(StateFilter(CheckinTestUpload), Command("cancel"))
@router.message(StateFilter(CheckinTestUpload), F.text == "Отмена")
async def cancel_checkin_test_upload(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(CheckinTestUpload.waiting_file, F.document)
async def checkin_test_file_step(message: types.Message, state: FSMContext, bot: Bot):
    if (message.document.file_size or 0) > _IMPORT_MAX_BYTES:
        await message.answer("Файл больше 20 МБ — столько я принять не могу.")
        return

    buf = await bot.download(message.document.file_id)
    buf.seek(0)
    text = decode_scan_export(buf.read())

    tag = await current_event_tag()
    records = find_checkin_records(text, tag)
    await state.set_state(None)

    from handlers.admin_sections import op_return_keyboard  # ленивый шов (см. docstring модуля)
    back_kb = await op_return_keyboard(message.from_user.id, "admin_checkin")

    if not records:
        await message.answer(
            "Не нашёл ни одного QR ЭТОГО форума в выгрузке — проверьте, что сканировали код "
            "именно этого события этим же приложением.",
            reply_markup=back_kb,
        )
        return

    total = len(records)
    readable_n = sum(1 for r in records if r["scanned_at"] is not None)
    if readable_n == total:
        time_line = "время скана читается"
    elif readable_n == 0:
        time_line = (
            "время скана НЕ читается — при настоящей загрузке бот подставит время самой "
            "загрузки файла вместо времени скана"
        )
    else:
        time_line = f"время скана читается у {readable_n} из {total}"

    await message.answer(
        f"✅ Приложение подходит: нашёл {total} QR форума, {time_line}.",
        reply_markup=back_kb,
    )


@router.message(CheckinTestUpload.waiting_file)
async def checkin_test_file_invalid(message: types.Message):
    await message.answer("Пришли файл выгрузки документом (не фото и не архив).")


# ── Форум-ночь п.3 (D-03, идея №2): ручной запуск рассылки QR + настройки города ────────────

async def _city_allowed(admin_id: int, code: str | None) -> bool:
    """WR-03-подобная проверка (тот же довод, что `handlers.admin_settings._cycle_enum_setting`
    для per-city ключей): менеджер, закреплённый за ОДНИМ городом (`staff.city`), не имеет
    права рассылать QR/трогать настройки ЧУЖОГО города, даже если соберёт `callback_data`
    вручную (кнопки в его собственном UI никогда не предлагают чужой город, но сам
    `callback_data` — не секрет). `code=None` (модуль городов выключен, или экран уже нацелен
    на единственную область без города) — разрешено всегда, других городов тогда не
    существует."""
    if code is None:
        return True
    import settings_ops
    return code in await settings_ops.per_city_visible_codes(admin_id)


_CITY_FORBIDDEN_ALERT = "Этот город правит суперадмин."


@router.callback_query(F.data.startswith("checkinqr_send:"))
async def checkinqr_send_confirm(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    blocked = await manual_send_block_reason(code)
    n = 0 if blocked else await pending_broadcast_count(code)
    if blocked or n == 0:
        await callback.answer(blocked or "Отправлять некому — все одобренные уже получили QR.",
                              show_alert=True)
        return
    label = await city_label(code) if code else None
    who = f"делегатам города {label}" if label else "делегатам"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, отправить", callback_data=f"checkinqr_send_go:{_encode_city(code)}"),
        InlineKeyboardButton(text="Отмена", callback_data="checkinqr_send_no"),
    ]])
    await callback.message.answer(f"Уйдёт {n} {who}. Отправить?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("checkinqr_send_go:"))
async def checkinqr_send_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    if blocked := await manual_send_block_reason(code):  # дату могли сменить после подтверждения
        await callback.answer(blocked, show_alert=True)
        return
    # T-12-03 (Rule 1): рассылка может занять минуты (сотни фото) — отвечаем на callback СРАЗУ
    # и правим то же сообщение, вместо того чтобы держать колбэк «в загрузке» до конца отправки
    # (Telegram считает такой колбэк протухшим и показывает тапнувшему ошибку).
    await callback.answer("Рассылка началась…")
    # Находка ревью 260924 (п.4): клавиатура «✅ Да, отправить»/«Отмена» убирается ДО запуска
    # (reply_markup=None ЯВНО — без него editMessageText сохраняет прежнюю разметку) —
    # повторный тап уже физически не по чему нажимать; send_broadcast вдобавок сама блокируется
    # per-city локом (двойной барьер: и на кнопке, и на сервисе — второй ловит гонку, которую
    # первый не успел бы, например форвард того же update).
    await callback.message.edit_text("⏳ Рассылаю QR...", reply_markup=None)
    result = await send_broadcast(code)
    if result.get("already_running"):
        report = "⏳ Рассылка этого города уже идёт — дождитесь её завершения."
    else:
        report = qr_send_report(result)
    try:  # итог — на месте «⏳ Рассылаю QR...», чтобы оно не висело
        await callback.message.edit_text(report, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(report, parse_mode="HTML")



@router.callback_query(F.data == "checkinqr_send_no")
async def checkinqr_send_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено. Ничего не отправлено.")
    await callback.answer()


async def _qr_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("checkin_qr_broadcast_enabled", code)
    ev_time = await get_setting_typed_for_city("checkin_qr_broadcast_time", code) or "18:00"
    morn_time = await get_setting_typed_for_city("checkin_qr_morning_repeat_time", code) or "08:00"
    catchup_until = await get_setting_typed_for_city("checkin_qr_morning_catchup_until", code) or "12:00"
    label = await city_label(code) if code else None
    on = enabled != "off"

    lines = ["⚙️ <b>Настройки QR</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Вечером (накануне форума): {ev_time}")
    lines.append(f"Утром (в день форума, неподтвердившим): {morn_time}")
    lines.append(f"Если бот не работал утром — догнать повтор до: {catchup_until}")
    # D-35 (24.09): QR — служебное сообщение, тихие часы на него не действуют (см.
    # services/checkin_broadcast.py) — предупреждение про тихие часы на этом экране больше не
    # нужно, время не может «увести» отправку в другое время.
    # D-35: автоотправка требует включённого master-тумблера «🎟 QR для чек-ина на форуме»
    # (checkin_qr_enabled, тот же гейт, что `services.checkin_broadcast.broadcast_enabled_for`)
    # — если он выключен, ни вечерняя, ни утренняя джоба не ставятся вовсе, даже когда рассылка
    # включена ЗДЕСЬ (per_city тумблер) — экран объясняет это словами, а не молча ничего не шлёт.
    if await get_setting_typed("checkin_qr_enabled") != "on":
        lines.append("\n⚠️ Вход по QR выключен — QR не рассылается.")
    elif await forum_date_for(code) is None:
        lines.append(
            "\n⚠️ «🗓 Дата начала форума» не задана — рассылка НЕ поставлена, даже если "
            "включена. Задайте её в разделе «🎪 Событие»."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"checkinqr_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🕕 Вечером: {ev_time}",
            callback_data=f"checkinqr_time:evening:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🌅 Утром: {morn_time}",
            callback_data=f"checkinqr_time:morning:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"⏳ Догонять повтор до: {catchup_until}",
            callback_data=f"checkinqr_time:catchup:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_checkin")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("checkinqr_cfg:"))
async def checkinqr_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _qr_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _safe_reschedule(code: str | None) -> None:
    """`schedule_city_jobs` требует запущенный планировщик (`services.scheduler.get_scheduler`
    бросает `RuntimeError`, если бот стартовал без него) — в проде это невозможно (main.py
    поднимает планировщик раньше, чем начинают приходить апдейты), но правка настройки не
    имеет права уронить сохранение ИЗ-ЗА этого; тот же fail-soft приём, что у
    `handlers.admin_settings._reschedule_checkin_qr_if_forum_date`."""
    try:
        await schedule_city_jobs(code)
    except Exception as e:
        logger.error(f"checkin_broadcast reschedule({code!r}) failed: {e}")


@router.callback_query(F.data.startswith("checkinqr_toggle:"))
async def checkinqr_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "checkin_qr_broadcast_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current != "off" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    await _safe_reschedule(code)
    text, kb = await _qr_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("checkinqr_time:"))
async def checkinqr_time_start(callback: types.CallbackQuery, state: FSMContext):
    _, which, raw_city = callback.data.split(":", 2)
    code = _decode_city(raw_city)
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key, example = {
        "evening": ("checkin_qr_broadcast_time", "18:00"),
        "catchup": ("checkin_qr_morning_catchup_until", "12:00"),
    }.get(which, ("checkin_qr_morning_repeat_time", "08:00"))
    await state.update_data(checkinqr_time_key=key, checkinqr_time_city=code)
    await state.set_state(CheckinQrTimeEdit.waiting_value)
    lead = (
        "До скольки догонять утренний повтор, если бот не работал в его время" if which == "catchup"
        else "Во сколько"
    )
    await callback.message.answer(
        f"{lead} (по времени города: «🕐 Часовой пояс», без него — московское)? Формат <code>ЧЧ:ММ</code>, например "
        f"<code>{example}</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(CheckinQrTimeEdit), Command("cancel"))
@router.message(StateFilter(CheckinQrTimeEdit), F.text == "Отмена")
async def cancel_checkinqr_time_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(CheckinQrTimeEdit.waiting_value)
async def checkinqr_time_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("checkinqr_time_key")
    code = data.get("checkinqr_time_city")
    await state.set_state(None)

    # Защитная перепроверка (T-092-01/WR-03 idiom): право на город могло измениться между
    # входом в FSM (checkinqr_time_start) и вводом значения — TOCTOU-гейт, тот же приём, что
    # handlers.admin_settings.settings_edit_value применяет к composite-ключам.
    if not await _city_allowed(message.from_user.id, code):
        await message.answer(_CITY_FORBIDDEN_ALERT, reply_markup=ReplyKeyboardRemove())
        return

    value, error = validate_setting_value(key, (message.text or "").strip())
    if error:
        await message.answer(error, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return
    if key == "checkin_qr_morning_catchup_until":
        morning = await get_setting_typed_for_city("checkin_qr_morning_repeat_time", code) or "08:00"
        if value <= morning:
            await message.answer(
                f"Это не позже утреннего повтора ({morning}) — догона не будет вовсе. Пришлите время "
                f"позже {morning}, например <code>12:00</code>.",
                parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
            )
            return

    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(message.from_user.id, composed, value)
    else:
        await set_setting_by_admin(message.from_user.id, key, value)
    await _safe_reschedule(code)

    text, kb = await _qr_cfg_text_kb(code)
    await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# Идеи №31/№32: журнал площадки (строки выше пишут в него перевыпуск QR и загрузку CSV) и снятие
# отметки менеджером — экраны в отдельном шве handlers/admin_venue.py, регистрируются здесь хвостом.
from services import venue_log as _venue_log  # noqa: E402
from handlers.admin_venue import venue_entry_rows as _venue_entry_rows  # noqa: E402
from services import checkin_training as _training  # noqa: E402
from handlers import admin_checkin_training  # noqa: E402,F401  (бэклог №7: «🧪 Учебные QR»)
