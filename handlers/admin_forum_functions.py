"""D-36 (владелец 24.09, `.planning/FORUM-CHECKIN.md` «Решения владельца 24.09 днём»): единый
экран «🎪 Форум: функции» — статус ВСЕХ форумных функций для города из шапки в одном месте,
вместо поиска менеджером по пяти разным разделам («✅ Отметки на форуме» / «🆘 SOS» / «🔘 Кнопки
меню» / «📱 Приложение» / «🗓 Программа форума»). Каждая строка — статус (✅/❌) + одна кнопка на
РОДНОЙ экран этой функции (переиспользуем существующий UI и его хендлеры, не дублируем логику
записи настройки нигде, кроме пункта ниже, у которого родного экрана раньше не было).

Плюс — недостающий экран «🎫 Шпаргалка волонтёра накануне форума»: тумблер
`checkin_volunteer_guide_broadcast_enabled` + время `checkin_volunteer_guide_broadcast_time`
(D-33) были в реестре настроек с самого начала, но БЕЗ экрана в боте (аудит D-36 нашёл дыру —
менеджер не мог поменять их иначе как руками в БД). Форма экрана — byte-в-byte
`handlers.admin_checkin._qr_cfg_text_kb`/`checkinqr_toggle_go`/`checkinqr_time_start`, только
один временной слот вместо двух (вечер/утро QR — у шпаргалки одна отправка).

Форма шва — та же, что у соседей (`handlers/session_feedback.py`, `handlers/admin_sos.py`):
своего `Router()` нет, `from handlers.admin import router`; импортирован из ХВОСТА
`handlers/admin.py`, СРАЗУ ПОСЛЕ `admin_sos` (golden-снапшот роутера требует чистого аппенда).
Право — `moderate_reg` (`handlers/admin_caps.py`), тот же довод, что у соседнего
`checkinqr_cfg:*` (массовая рассылка + правка расписания, не рутинное сканирование волонтёра —
то, для чего достаточно `checkin`). Отдельные строки хаба ведут на экраны с ДРУГИМИ, порой более
узкими капами (`admin_checkin` — `checkin`, `admin_menu_buttons`/`admin_miniapp_settings` —
`settings`) — та же развилка «вход широкий, действие узкое», что у кнопки «⚙️ Настройки QR» на
`admin_checkin` (виден держателю `checkin`, тапнуть может только `moderate_reg`)."""
import html

from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import (
    cities_module_on,
    city_label,
    default_city_code,
    enabled_cities,
    get_setting_typed_for_city,
    per_city_key,
)
from database.db import has_program_sessions_for_city
from handlers.admin import router
from handlers.admin_caps import _holds, required_capability, resolve_capabilities
from handlers.admin_checkin import (
    _CITY_FORBIDDEN_ALERT,
    _admin_city_scope,
    _city_allowed,
    _decode_city,
    _encode_city,
)
from handlers.admin_sections import back_button
from handlers.states import CheckinVolGuideTimeEdit
from keyboards.builders import get_cancel_kb
from services import session_feedback as sf
from services.checkin_volunteer_broadcast import schedule_city_job as schedule_volunteer_guide_job
from services.sos import is_sos_active_for_city
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed
from settings_validation import validate_setting_value

async def _resolve_screen_city(admin_id: int) -> str | None:
    """Тот же трёхветочный резолвер «город из шапки», что `handlers.admin_checkin.
    _resolve_checkin_screen_city`/`handlers.admin_program._resolve_city_for_screen` (не
    импортирую напрямую — оба приватные к своему модулю, но логика byte-в-byte, копия дешевле
    межмодульной приватной связи ещё и с admin_program)."""
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        return own_scope[0]
    if not await cities_module_on():
        return default_city_code()
    return None


def _status(on: bool) -> str:
    return "✅ Вкл" if on else "❌ Выкл"


# Хаб открывается под `moderate_reg` (ADMIN_CAPS["admin_forum_functions"]), но каждая строка
# ведёт на РОДНОЙ экран своей функции под СВОЕЙ капой (`toggle_checkin_qr_enabled`/
# `admin_miniapp_settings`/`admin_menu_buttons`/`prog_fbset:*` — все `settings`; `admin_checkin`
# — `checkin`; SOS и рассылки QR/шпаргалки — тот же `moderate_reg`, что у самого хаба). Держатель
# только `moderate_reg` тапнул бы «📱 Настройки приложения» и получил бы «Недостаточно прав» —
# рисовать кнопку, на которую нельзя нажать, только сбивает с толку («бот для людей»: не
# показываем недоступное). Статусная СТРОКА (✅/❌) остаётся видна всегда — это же тот факт,
# который хаб и обещает показать «в одном месте», её читает и держатель одной `moderate_reg`.
async def _render_hub(admin_id: int, code: str) -> tuple[str, InlineKeyboardMarkup]:
    caps = await resolve_capabilities(admin_id)

    def visible(callback_data: str) -> bool:
        cap = required_capability(callback_data=callback_data)
        return cap is not None and _holds(caps, cap)

    label = await city_label(code) if await cities_module_on() else None
    lines = ["🎪 <b>Форум: функции</b>" + (f" — {html.escape(label)}" if label else ""), ""]
    buttons: list[list[InlineKeyboardButton]] = []

    # 1. Выпуск личного QR — мастер-тумблер, НЕ per_city (services/checkin.py::build_checkin_qr
    # читает его глобально); правится строкой «toggle_checkin_qr_enabled» раздела «📋 Заявки».
    qr_on = await get_setting_typed("checkin_qr_enabled") == "on"
    lines.append(f"🎟 QR для входа: {_status(qr_on)}")
    if visible("toggle_checkin_qr_enabled"):
        buttons.append([InlineKeyboardButton(text="🎟 Включить/выключить QR", callback_data="toggle_checkin_qr_enabled")])

    # 2. Рассылка QR накануне + утренний повтор — per_city, родной экран уже есть.
    qr_bc_on = await get_setting_typed_for_city("checkin_qr_broadcast_enabled", code) == "on"
    lines.append(f"🎟 Рассылка QR накануне + утренний повтор: {_status(qr_bc_on)}")
    if visible(f"checkinqr_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🎟 Настройки рассылки QR", callback_data=f"checkinqr_cfg:{_encode_city(code)}",
        )])

    # 3. Отметки на форуме — загрузка CSV офлайн-сканера + «📨 Написать не пришедшим». Ручные
    # действия под правом `checkin`, тумблера не требуют (D-36: тумблер нужен ФУНКЦИЯМ, которые
    # работают САМИ — ручная кнопка и так «выключена», пока никто не нажал).
    lines.append("✅ Отметки на форуме (загрузка CSV, «Не пришли»): ручная кнопка, без тумблера")
    if visible("admin_checkin"):
        buttons.append([InlineKeyboardButton(text="✅ Отметки на форуме", callback_data="admin_checkin")])

    # 4. Сканер в Mini App — мастер-тумблер miniapp_section_checkin, НЕ per_city, правится на
    # экране «📱 Приложение» (там же общий master miniapp_enabled — сканер без него не откроется
    # ни при каком miniapp_section_checkin, поэтому ссылаемся на экран целиком, не дублируем).
    miniapp_on = await get_setting_typed("miniapp_section_checkin") == "on"
    lines.append(f"🎫 Сканер в приложении: {_status(miniapp_on)}")
    if visible("admin_miniapp_settings"):
        buttons.append([InlineKeyboardButton(text="📱 Настройки приложения", callback_data="admin_miniapp_settings")])

    # 5. Шпаргалка волонтёра накануне форума — per_city, НОВЫЙ экран этого модуля (был
    # недостающим по аудиту D-36).
    vol_on = await get_setting_typed_for_city("checkin_volunteer_guide_broadcast_enabled", code) == "on"
    lines.append(f"🎫 Шпаргалка волонтёра накануне форума: {_status(vol_on)}")
    if visible(f"checkinvol_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🎫 Настройки шпаргалки", callback_data=f"checkinvol_cfg:{_encode_city(code)}",
        )])

    # 6. Программа (кнопка делегата, D-29) — гейт «есть фото (своё городское ИЛИ общее) ИЛИ
    # хотя бы одна сессия» решает keyboards.builders.get_main_menu_kb на лету для каждого
    # делегата (services.program.has_program_content, общая точка правды); здесь показываем
    # только часть гейта, которую видно БЕЗ конкретного делегата — есть ли сессии в программе
    # города (фото — экран «🎪 Событие»/строка ниже, не тумблер).
    has_sessions = await has_program_sessions_for_city(code)
    lines.append(f"🗓 Программа (сессии заведены): {_status(has_sessions)}")
    if visible("admin_menu_buttons"):
        buttons.append([InlineKeyboardButton(
            text="🔘 Кнопки меню (Программа/Важное/SOS/QR)", callback_data="admin_menu_buttons",
        )])
    # D-29: что делегат видит по этой кнопке в Mini App — таблица сессий бота или фото. Общий
    # рендер с экраном «🗓 Программа форума» — handlers/admin_program_view.py.
    from handlers.admin_program_view import program_view_row
    view_status, view_button = await program_view_row(code, "hub")
    lines.append(view_status)
    if visible(view_button.callback_data):
        buttons.append([view_button])

    # 7. Отзывы о сессиях — per_city, родной экран уже есть (handlers/session_feedback.py).
    fb_on = await sf.is_enabled_for_city(code)
    lines.append(f"⭐ Отзывы о сессиях: {_status(fb_on)}")
    if visible(f"prog_fbset:{code}"):
        buttons.append([InlineKeyboardButton(text="⭐ Настройки отзывов", callback_data=f"prog_fbset:{code}")])

    # 8. SOS — сама кнопка меню (menu_sos, см. пункт 6) + окно активности (forum_date +
    # sos_active_days); родной экран уже есть (handlers/admin_sos.py).
    sos_on = await is_sos_active_for_city(code)
    lines.append(f"🆘 SOS активен сейчас: {_status(sos_on)}")
    if visible("admin_sos"):
        buttons.append([InlineKeyboardButton(text="🆘 Настройки SOS", callback_data="admin_sos")])

    # 9. «🔕 Не присылать сегодня» — D-30: доступна делегату весь сезон намеренно, без
    # мастер-тумблера (отключать самообслуживание делегата — не то, что просил владелец).
    # Информационная строка, без кнопки.
    lines.append("🔕 «Не присылать сегодня» у делегата: всегда доступна (весь сезон, D-30)")

    if not await cities_module_on():
        lines.append("\n<i>Модуль городов выключен — показаны общие (не городские) значения.</i>")

    buttons.append([back_button("admin_checkin", "◀️ Назад")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_city_picker() -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"forumfn_city:{c['code']}")]
        for c in await enabled_cities()
    ]
    buttons.append([back_button("admin_checkin", "◀️ Назад")])
    return "🎪 <b>Форум: функции</b>\n\nВыберите город.", InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_forum_functions")
async def admin_forum_functions_entry(callback: types.CallbackQuery):
    code = await _resolve_screen_city(callback.from_user.id)
    if code is None:
        text, kb = await _render_city_picker()
    else:
        text, kb = await _render_hub(callback.from_user.id, code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumfn_city:"))
async def admin_forum_functions_city_pick(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _render_hub(callback.from_user.id, code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Шпаргалка волонтёра накануне форума (D-33/D-36) — недостающий экран ─────────────────────
# Форма byte-в-byte `handlers.admin_checkin._qr_cfg_text_kb`/`checkinqr_toggle_go`/
# `checkinqr_time_start`, один временной слот вместо двух.

async def _vol_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("checkin_volunteer_guide_broadcast_enabled", code)
    t = await get_setting_typed_for_city("checkin_volunteer_guide_broadcast_time", code) or "17:00"
    label = await city_label(code) if code else None
    on = enabled != "off"

    lines = ["🎫 <b>Шпаргалка волонтёра накануне форума</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Время (накануне форума): {t}")
    text_set = bool((await get_setting_typed("checkin_volunteer_guide_text") or "").strip())
    if not text_set:
        lines.append("\n⚠️ Текст шпаргалки пуст — рассылка НЕ поставлена, даже если включена здесь.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"checkinvol_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🕕 Время: {t}",
            callback_data=f"checkinvol_time:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("checkinvol_cfg:"))
async def checkinvol_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _vol_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _safe_reschedule_vol(code: str | None) -> None:
    """Fail-soft перепостановка джобы — тот же приём, что `handlers.admin_checkin.
    _safe_reschedule`: правка настройки не имеет права уронить сохранение из-за недоступного
    планировщика (например, в тесте без запущенного `AsyncIOScheduler`)."""
    import logging
    try:
        await schedule_volunteer_guide_job(code)
    except Exception as e:
        logging.getLogger(__name__).error(f"checkin_volunteer_broadcast reschedule({code!r}) failed: {e}")


@router.callback_query(F.data.startswith("checkinvol_toggle:"))
async def checkinvol_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "checkin_volunteer_guide_broadcast_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current != "off" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    await _safe_reschedule_vol(code)
    text, kb = await _vol_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("checkinvol_time:"))
async def checkinvol_time_start(callback: types.CallbackQuery, state: FSMContext):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.update_data(checkinvol_time_city=code)
    await state.set_state(CheckinVolGuideTimeEdit.waiting_value)
    await callback.message.answer(
        "Во сколько НАКАНУНЕ форума (московское время)? Формат <code>ЧЧ:ММ</code>, например "
        "<code>17:00</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(CheckinVolGuideTimeEdit), Command("cancel"))
@router.message(StateFilter(CheckinVolGuideTimeEdit), F.text == "Отмена")
async def cancel_checkinvol_time_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(CheckinVolGuideTimeEdit.waiting_value)
async def checkinvol_time_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("checkinvol_time_city")
    await state.set_state(None)

    if not await _city_allowed(message.from_user.id, code):
        await message.answer(_CITY_FORBIDDEN_ALERT, reply_markup=ReplyKeyboardRemove())
        return

    key = "checkin_volunteer_guide_broadcast_time"
    value, error = validate_setting_value(key, (message.text or "").strip())
    if error:
        await message.answer(error, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(message.from_user.id, composed, value)
    else:
        await set_setting_by_admin(message.from_user.id, key, value)
    await _safe_reschedule_vol(code)

    text, kb = await _vol_cfg_text_kb(code)
    await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
