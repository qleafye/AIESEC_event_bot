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
from handlers.admin import router
from handlers.admin_caps import _holds, has_capability, required_capability, resolve_capabilities
from handlers.admin_checkin import (
    _CITY_FORBIDDEN_ALERT,
    _admin_city_scope,
    _city_allowed,
    _decode_city,
    _encode_city,
)
from handlers.admin_checkin_training import sheet_allowed
from handlers.admin_sections import back_button
from handlers.states import CheckinVolGuideTimeEdit
from handlers.states import ForumDayMenuTimeEdit
from handlers.states import ForumDayReportTimeEdit, ForumNoshowPollTimeEdit
from handlers.states import RegionalNoshowMoveTimeEdit
from keyboards.builders import get_cancel_kb
from services import session_feedback as sf
from services.checkin_volunteer_broadcast import schedule_city_job as schedule_volunteer_guide_job
from services.forum_day_menu import is_forum_day_menu_active_for_city
from services import forum_day_report as fdr
from services import forum_noshow_poll as fnsp
from services import regional_noshow_move as rgnm
from services.forum_day_report import schedule_city_job as schedule_day_report_job
from services.forum_noshow_poll import schedule_city_job as schedule_noshow_poll_job
from services.reject_rules import forum_date_for
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
    # Бэклог №25: светофор «всё ли готово сейчас» — handlers/admin_forum_ready.py.
    if visible(f"forum_ready:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(text="🚦 Готовность к форуму", callback_data=f"forum_ready:{_encode_city(code)}")])

    # 1. Выпуск личного QR — общий тумблер, НЕ per_city; из хаба — только через подтверждение.
    qr_on = await get_setting_typed("checkin_qr_enabled") == "on"
    lines.append(f"🎟 QR для входа: {_status(qr_on)}")
    from handlers.admin_forum_hub_nav import sees_all_cities  # тумблер общий: кнопка — только видящим все города
    if visible(f"forumfn_qr:{_encode_city(code)}") and await sees_all_cities(admin_id):  # подтверждение: admin_forum_hub_nav.py
        buttons.append([InlineKeyboardButton(text="🎟 Вход по QR (общий для всех городов)", callback_data=f"forumfn_qr:{_encode_city(code)}")])

    # 2. Рассылка QR накануне + утренний повтор — per_city, родной экран уже есть.
    qr_bc_on = await get_setting_typed_for_city("checkin_qr_broadcast_enabled", code) == "on"
    lines.append(f"🎟 Рассылка QR накануне + утренний повтор: {_status(qr_bc_on)}")
    if visible(f"checkinqr_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🎟 Настройки рассылки QR", callback_data=f"checkinqr_cfg:{_encode_city(code)}",
        )])

    from services.timeutil import city_offset_hours, offset_label  # «🕐 Часовой пояс» города
    lines.append(f"🕐 Часовой пояс: {offset_label(await city_offset_hours(code))}")
    if visible(f"forumtz_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(text="🕐 Часовой пояс", callback_data=f"forumtz_cfg:{_encode_city(code)}")])

    # 3. Отметки на форуме — загрузка CSV офлайн-сканера + «📨 Написать не пришедшим». Ручные
    # действия под правом `checkin`, тумблера не требуют (D-36: тумблер нужен ФУНКЦИЯМ, которые
    # работают САМИ — ручная кнопка и так «выключена», пока никто не нажал).
    lines.append("✅ Отметки на форуме (загрузка CSV, «Не пришли»): ручная кнопка, без тумблера")
    if visible(f"forumfn_open:chk:{_encode_city(code)}"):  # «Назад» оттуда — в хаб (admin_forum_hub_nav)
        buttons.append([InlineKeyboardButton(text="✅ Отметки на форуме", callback_data=f"forumfn_open:chk:{_encode_city(code)}")])
    # Бэклог чек-ина №7: лист учебных QR — менеджер с одним moderate_reg готовит волонтёров.
    lines.append("🧪 Учебные QR для тренировки волонтёров: лист A4, ничего не записывают")
    if sheet_allowed(caps):
        buttons.append([InlineKeyboardButton(text="🧪 Учебные QR", callback_data="checkin_training_sheet")])

    # 4. Сканер в Mini App — мастер-тумблер miniapp_section_checkin, НЕ per_city, правится на
    # экране «📱 Приложение» (там же общий master miniapp_enabled — сканер без него не откроется
    # ни при каком miniapp_section_checkin, поэтому ссылаемся на экран целиком, не дублируем).
    miniapp_on = await get_setting_typed("miniapp_section_checkin") == "on"
    lines.append(f"🎫 Сканер в приложении: {_status(miniapp_on)}")
    if visible(f"forumfn_open:app:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(text="📱 Настройки приложения", callback_data=f"forumfn_open:app:{_encode_city(code)}")])

    # 5. Шпаргалка волонтёра накануне форума — per_city, НОВЫЙ экран этого модуля (был
    # недостающим по аудиту D-36).
    vol_on = await get_setting_typed_for_city("checkin_volunteer_guide_broadcast_enabled", code) == "on"
    lines.append(f"🎫 Шпаргалка волонтёра накануне форума: {_status(vol_on)}")
    if visible(f"checkinvol_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🎫 Настройки шпаргалки", callback_data=f"checkinvol_cfg:{_encode_city(code)}",
        )])

    # Идея №5 бэклога чек-ина: приглашение волонтёров ссылкой — per_city, свой экран
    # (handlers/admin_volunteer_invite.py). Строка добавлена аддитивно (RULES.md), номер шага
    # соседей выше не переставляется.
    volinv_on = await get_setting_typed_for_city("volunteer_invite_enabled", code) == "on"
    lines.append(f"🔗 Приглашение волонтёров ссылкой: {_status(volinv_on)}")
    if visible(f"volinvite_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🔗 Пригласить волонтёров", callback_data=f"volinvite_cfg:{_encode_city(code)}",
        )])

    # Идея №20 бэклога чек-ина: бюро находок — per_city, свой экран
    # (handlers/admin_lost_found.py). Строка добавлена аддитивно (RULES.md), номер шага
    # соседей выше не переставляется.
    lostfound_on = await get_setting_typed_for_city("lost_found_enabled", code) == "on"
    lines.append(f"🧳 Бюро находок: {_status(lostfound_on)}")
    if visible(f"lostfound_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🧳 Настройки бюро находок", callback_data=f"lostfound_cfg:{_encode_city(code)}",
        )])

    # D-41 (регистрация на месте): короткая анкета по QR у стойки — per_city, свой экран
    # (handlers/admin_onsite_reg.py). Строка добавлена аддитивно, соседи не переставлены.
    from services.onsite_reg import onsite_enabled  # строго по городу, без общего ключа
    onsite_on = await onsite_enabled(code)
    lines.append(f"📝 Регистрация на месте: {_status(onsite_on)}")
    if visible(f"onsitereg_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="📝 Регистрация на месте", callback_data=f"onsitereg_cfg:{_encode_city(code)}",
        )])

    # 6. Программа (кнопка делегата, D-29) — статус ровно тот, что у меню делегата и Mini App:
    # `services.program.program_menu_visible` (тумблер menu_program города И есть фото или
    # сессии). Раньше строка смотрела только на сессии и писала «Вкл» при выключенной кнопке.
    from services.program import program_menu_visible
    program_line = "📅 Кнопка «Программа» у делегата: "
    if await program_menu_visible(code):
        lines.append(program_line + _status(True))
    elif await get_setting_typed_for_city("menu_program", code) != "on":
        lines.append(program_line + _status(False) + " (выключена в «Кнопках меню»)")
    else:
        lines.append(program_line + _status(False) + " (нет ни фото, ни сессий)")
    if visible(f"forumfn_open:menu:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="🔘 Кнопки меню (Программа/Важное/SOS/QR)", callback_data=f"forumfn_open:menu:{_encode_city(code)}",
        )])
    # D-29: что делегат видит по этой кнопке в Mini App — таблица сессий бота или фото. Общий
    # рендер с экраном «🗓 Программа форума» — handlers/admin_program_view.py.
    from handlers.admin_program_view import program_rows  # + фото программы города
    view_status, view_rows = await program_rows(code, "hub")
    lines.append(view_status)
    buttons += [row for row in view_rows if visible(row[0].callback_data)]

    # 7. Отзывы о сессиях — per_city, родной экран уже есть (handlers/session_feedback.py).
    fb_on = await sf.is_enabled_for_city(code)
    lines.append(f"⭐ Отзывы о сессиях: {_status(fb_on)}")
    if visible(f"forumfn_open:fb:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(text="⭐ Настройки отзывов", callback_data=f"forumfn_open:fb:{_encode_city(code)}")])

    from services.session_enroll import module_enabled as _enroll_on  # запись на сессии (admin_enroll_list)
    lines.append(f"📅 Запись на сессии: {_status(await _enroll_on(code))}")
    if visible(f"prog_enrset:{code}"):
        buttons.append([InlineKeyboardButton(text="📅 Настройки записи", callback_data=f"prog_enrset:{code}")])
    from database import quiz_db as _qdb  # тест компетенций (admin_quiz)
    _quiz = await _qdb.get_quiz_for_city(code)
    lines.append(f"🧭 Тест компетенций: {_status(bool(_quiz and _quiz['enabled']))}")
    if visible(f"prog_qz:{code}"):
        buttons.append([InlineKeyboardButton(text="🧭 Тест компетенций", callback_data=f"prog_qz:{code}")])

    # 8. SOS — сама кнопка меню (menu_sos, см. пункт 6) + окно активности (forum_date +
    # sos_active_days); родной экран уже есть (handlers/admin_sos.py).
    sos_on = await is_sos_active_for_city(code)
    lines.append(f"🆘 SOS активен сейчас: {_status(sos_on)}")
    if visible(f"asos_city:{code}"):
        buttons.append([InlineKeyboardButton(text="🆘 Настройки SOS", callback_data=f"asos_city:{code}")])

    # 9. «🔕 Не присылать сегодня» — D-30: доступна делегату весь сезон намеренно, без
    # мастер-тумблера (отключать самообслуживание делегата — не то, что просил владелец).
    # Информационная строка, без кнопки.
    lines.append("🔕 «Не присылать сегодня» у делегата: всегда доступна (весь сезон)")

    # 10. Идея №1 бэклога чек-ина: режим «день форума» главного меню делегата — тумблер +
    # окно активности (forum_date + sos_active_days, вечер накануне); родной экран заведён
    # этим же модулем (forumdaymenu_cfg:*). Строка видна только держателю права на СВОЙ
    # экран (moderate_reg — тот же капа, что и у входа в этот хаб) — тот же приём, что
    # handlers.admin_sections.visible_rows использует для строк раздела (капа берётся из
    # ADMIN_CAPS, а не второй самодельной картой).
    if await has_capability(admin_id, "moderate_reg"):
        fdm_on = await get_setting_typed_for_city("forum_day_menu_enabled", code) == "on"
        fdm_line = _status(fdm_on)
        if fdm_on:
            fdm_active = await is_forum_day_menu_active_for_city(code)
            fdm_line += " (сегодня форум — меню форумное)" if fdm_active else " (сейчас обычное меню)"
        lines.append(f"📱 Меню «день форума»: {fdm_line}")
        buttons.append([InlineKeyboardButton(
            text="📱 Настройки меню «день форума»", callback_data=f"forumdaymenu_cfg:{_encode_city(code)}",
        )])

        # 11. Идея №3 бэклога чек-ина: приветствие после первой отметки входа делегата —
        # тумблер + текст (текст правится общим текстовым редактором «📋 Заявки», тот же приём,
        # что у соседей checkin_qr_broadcast_text/checkin_not_arrived_text). Родной экран —
        # этот же модуль (forumwelcome_cfg:*).
        welcome_on = await get_setting_typed_for_city("forum_welcome_enabled", code) == "on"
        lines.append(f"👋 Приветствие после отметки на входе: {_status(welcome_on)}")
        buttons.append([InlineKeyboardButton(
            text="👋 Настройки приветствия после отметки", callback_data=f"forumwelcome_cfg:{_encode_city(code)}",
        )])

        # Идея №16 бэклога чек-ина: отчёт дня форума вечером — тумблер + время, свой экран
        # этого же модуля (forumdayreport_cfg:*).
        report_on = await get_setting_typed_for_city("forum_day_report_enabled", code) == "on"
        lines.append(f"📊 Отчёт дня форума вечером: {_status(report_on)}")
        buttons.append([InlineKeyboardButton(
            text="📊 Настройки отчёта дня форума", callback_data=f"forumdayreport_cfg:{_encode_city(code)}",
        )])

        # Идея №23 бэклога чек-ина: опрос неявившихся «почему не пришёл» — тумблер + время,
        # свой экран этого же модуля (forumnoshowpoll_cfg:*).
        poll_on = await get_setting_typed_for_city("forum_noshow_poll_enabled", code) == "on"
        lines.append(f"❓ Опрос неявившихся «почему не пришёл»: {_status(poll_on)}")
        buttons.append([InlineKeyboardButton(
            text="❓ Настройки опроса неявившихся", callback_data=f"forumnoshowpoll_cfg:{_encode_city(code)}",
        )])

        # Трек «региональные форумы → Москва»: перенос неявившихся — тумблер + время + город
        # назначения + статус после переноса, свой экран этого же модуля (rgnm_cfg:*).
        rgnm_on = await get_setting_typed_for_city("regional_noshow_offer_enabled", code) == "on"
        lines.append(f"🚌 Перенос неявившихся на форум в Москве: {_status(rgnm_on)}")
        buttons.append([InlineKeyboardButton(
            text="🚌 Настройки переноса в Москву", callback_data=f"rgnm_cfg:{_encode_city(code)}",
        )])

    # Идея №29 бэклога чек-ина («Твой Юлид в цифрах») — картинка-итог делегату после форума,
    # per_city, свой экран этого же трека (handlers/admin_forum_stats_card.py, forumstats_cfg:*).
    # Строка добавлена аддитивно (RULES.md), номер шага соседей выше не переставляется.
    stats_card_on = await get_setting_typed_for_city("forum_stats_card_enabled", code) == "on"
    lines.append(f"📊 Карточка «Мы в цифрах»: {_status(stats_card_on)}")
    if visible(f"forumstats_cfg:{_encode_city(code)}"):
        buttons.append([InlineKeyboardButton(
            text="📊 Настройки карточки «Мы в цифрах»", callback_data=f"forumstats_cfg:{_encode_city(code)}",
        )])

    if not await cities_module_on():
        lines.append("\n<i>Модуль городов выключен — показаны общие (не городские) значения.</i>")

    buttons.append([back_button("admin_forum_functions", "◀️ Назад")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_city_picker() -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"forumfn_city:{c['code']}")]
        for c in await enabled_cities()
    ]
    buttons.append([back_button("admin_forum_functions", "◀️ Назад")])
    return "🎪 <b>Форум: функции</b>\n\nВыберите город.", InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_forum_functions")
async def admin_forum_functions_entry(callback: types.CallbackQuery):
    code = await _resolve_screen_city(callback.from_user.id)
    if code is None:
        text, kb = await _render_city_picker()
    else:
        text, kb = await _render_hub(callback.from_user.id, code)
    from handlers.admin_forum_hub_nav import edit_or_answer  # правкой, а не новым сообщением
    await edit_or_answer(callback, text, kb)
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


# ── Идея №1 бэклога чек-ина: режим «день форума» главного меню делегата ─────────────────────
# Форма byte-в-byte `_vol_cfg_text_kb`/`checkinvol_toggle_go`/`checkinvol_time_start` выше —
# тумблер + один временной слот («вечером накануне»), без джобы для переставления (не
# APScheduler-фича — `services.forum_day_menu` резолвится живьём на каждом рендере меню, тут
# перепланировать нечего).

async def _forumdaymenu_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("forum_day_menu_enabled", code)
    t = await get_setting_typed_for_city("forum_day_menu_start_time", code) or "18:00"
    label = await city_label(code) if code else None
    on = enabled == "on"

    lines = ["📱 <b>Меню «день форума»</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Режим: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Начало (вечером накануне форума): {t}")
    if on:
        active_now = await is_forum_day_menu_active_for_city(code)
        lines.append("Сейчас: 🎪 форумное меню" if active_now else "Сейчас: обычное меню")
    forum_date_set = await forum_date_for(code) is not None
    if not forum_date_set:
        lines.append("\n⚠️ «🗓 Дата начала форума» не задана — режим не включится, даже если Вкл здесь.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Режим: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"forumdaymenu_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🕕 Начало: {t}",
            callback_data=f"forumdaymenu_time:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumdaymenu_cfg:"))
async def forumdaymenu_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _forumdaymenu_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumdaymenu_toggle:"))
async def forumdaymenu_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "forum_day_menu_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _forumdaymenu_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("forumdaymenu_time:"))
async def forumdaymenu_time_start(callback: types.CallbackQuery, state: FSMContext):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.update_data(forumdaymenu_time_city=code)
    await state.set_state(ForumDayMenuTimeEdit.waiting_value)
    await callback.message.answer(
        "Во сколько ВЕЧЕРОМ НАКАНУНЕ форума (московское время) включать форумное меню? "
        "Формат <code>ЧЧ:ММ</code>, например <code>18:00</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(ForumDayMenuTimeEdit), Command("cancel"))
@router.message(StateFilter(ForumDayMenuTimeEdit), F.text == "Отмена")
async def cancel_forumdaymenu_time_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(ForumDayMenuTimeEdit.waiting_value)
async def forumdaymenu_time_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("forumdaymenu_time_city")
    await state.set_state(None)

    if not await _city_allowed(message.from_user.id, code):
        await message.answer(_CITY_FORBIDDEN_ALERT, reply_markup=ReplyKeyboardRemove())
        return

    key = "forum_day_menu_start_time"
    value, error = validate_setting_value(key, (message.text or "").strip())
    if error:
        await message.answer(error, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(message.from_user.id, composed, value)
    else:
        await set_setting_by_admin(message.from_user.id, key, value)

    text, kb = await _forumdaymenu_cfg_text_kb(code)
    await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Идея №3 бэклога чек-ина: приветствие после первой отметки входа делегата ────────────────
# Тумблер-only экран (в отличие от соседей выше — нет своего временного поля): САМ текст
# (`forum_welcome_text`) правится общим текстовым редактором «📋 Заявки» (`settings_edit:*`,
# капа «settings» — попадание в `_APPS_FIELD_ORDER`, `handlers/admin_settings.py`), тот же
# приём, что у соседей `checkin_qr_broadcast_text`/`checkin_not_arrived_text`; здесь — только
# тумблер (капа «moderate_reg», тот же довод, что у остального хаба).

async def _welcome_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("forum_welcome_enabled", code)
    label = await city_label(code) if code else None
    on = enabled == "on"

    lines = ["👋 <b>Приветствие после отметки на входе</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Отправка: {'✅ Вкл' if on else '❌ Выкл'}")
    text_set = bool((await get_setting_typed_for_city("forum_welcome_text", code) or "").strip())
    if not text_set:
        lines.append("\n⚠️ Текст приветствия пуст — отправка НЕ пойдёт, даже если включена здесь.")
    lines.append("\nТекст правится в «⚙️ Настройки» → «📋 Заявки» → «👋 Текст приветствия после отметки на входе».")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Отправка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"forumwelcome_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumwelcome_cfg:"))
async def forumwelcome_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _welcome_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumwelcome_toggle:"))
async def forumwelcome_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "forum_welcome_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _welcome_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


# ── Идея №16 бэклога чек-ина: «📊 Отчёт дня форума» вечером ──────────────────────────────────
# Форма — тот же приём, что «Шпаргалка волонтёра» выше (тумблер + время), плюс две ручные
# кнопки: «Отчёт дня сейчас» (не трогает идемпотентность автоматической джобы) и «Выгрузить
# отметки (CSV)».

async def _safe_reschedule_day_report(code: str | None) -> None:
    import logging
    try:
        await schedule_day_report_job(code)
    except Exception as e:
        logging.getLogger(__name__).error(f"forum_day_report reschedule({code!r}) failed: {e}")


async def _day_report_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("forum_day_report_enabled", code)
    t = await get_setting_typed_for_city("forum_day_report_time", code) or "21:00"
    label = await city_label(code) if code else None
    on = enabled == "on"

    lines = ["📊 <b>Отчёт дня форума</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Время (каждый день форума): {t}")
    forum_date_set = await forum_date_for(code) is not None
    if not forum_date_set:
        lines.append("\n⚠️ «🗓 Дата начала форума» не задана — отчёт не поставится, даже если Вкл здесь.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"forumdayreport_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🕕 Время: {t}",
            callback_data=f"forumdayreport_time:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="📊 Отчёт дня сейчас", callback_data=f"forumdayreport_now:{_encode_city(code)}")],
        [InlineKeyboardButton(text="📥 Выгрузить отметки (CSV)", callback_data=f"forumdayreport_csv:{_encode_city(code)}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumdayreport_cfg:"))
async def forumdayreport_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _day_report_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumdayreport_toggle:"))
async def forumdayreport_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "forum_day_report_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    await _safe_reschedule_day_report(code)
    text, kb = await _day_report_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("forumdayreport_time:"))
async def forumdayreport_time_start(callback: types.CallbackQuery, state: FSMContext):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.update_data(forumdayreport_time_city=code)
    await state.set_state(ForumDayReportTimeEdit.waiting_value)
    await callback.message.answer(
        "Во сколько КАЖДЫЙ день форума слать отчёт дня (московское время)? Формат "
        "<code>ЧЧ:ММ</code>, например <code>21:00</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(ForumDayReportTimeEdit), Command("cancel"))
@router.message(StateFilter(ForumDayReportTimeEdit), F.text == "Отмена")
async def cancel_forumdayreport_time_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(ForumDayReportTimeEdit.waiting_value)
async def forumdayreport_time_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("forumdayreport_time_city")
    await state.set_state(None)

    if not await _city_allowed(message.from_user.id, code):
        await message.answer(_CITY_FORBIDDEN_ALERT, reply_markup=ReplyKeyboardRemove())
        return

    key = "forum_day_report_time"
    value, error = validate_setting_value(key, (message.text or "").strip())
    if error:
        await message.answer(error, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(message.from_user.id, composed, value)
    else:
        await set_setting_by_admin(message.from_user.id, key, value)
    await _safe_reschedule_day_report(code)

    text, kb = await _day_report_cfg_text_kb(code)
    await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("forumdayreport_now:"))
async def forumdayreport_now_go(callback: types.CallbackQuery):
    """Ручной запуск «📊 Отчёт дня сейчас» — считает и шлёт отчёт СЕГОДНЯШНЕГО дня, НЕ трогая
    идемпотентность автоматической вечерней джобы (`mark_sent=False`, докстринг
    `services/forum_day_report.py`)."""
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    from services.timeutil import msk_now
    today = msk_now().strftime("%Y-%m-%d")
    await callback.answer("Считаю отчёт…")
    try:
        result = await fdr.send_report(code, today, mark_sent=False)
    except Exception as e:
        await callback.message.answer(f"⚠️ Не удалось собрать отчёт: {e}")
        return
    await callback.message.answer(
        f"Отправлено: чат {'✅' if result['chat_delivered'] else '—'}, "
        f"лично {result['dm_delivered']} держателям."
    )


@router.callback_query(F.data.startswith("forumdayreport_csv:"))
async def forumdayreport_csv_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    from aiogram.types import BufferedInputFile
    from services.timeutil import msk_now

    today = msk_now().strftime("%Y-%m-%d")
    await callback.answer()
    csv_bytes = await fdr.checkins_csv_for_city_day(code, today)
    document = BufferedInputFile(csv_bytes, filename=f"checkins_{code or 'all'}_{today}.csv")
    await callback.message.answer_document(document, caption=f"Отметки за {today}")


# ── Идея №23 бэклога чек-ина: опрос неявившихся «почему не пришёл» ──────────────────────────

async def _safe_reschedule_noshow_poll(code: str | None) -> None:
    import logging
    try:
        await schedule_noshow_poll_job(code)
    except Exception as e:
        logging.getLogger(__name__).error(f"forum_noshow_poll reschedule({code!r}) failed: {e}")


async def _noshow_poll_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("forum_noshow_poll_enabled", code)
    t = await get_setting_typed_for_city("forum_noshow_poll_time", code) or "12:00"
    label = await city_label(code) if code else None
    on = enabled == "on"

    lines = ["❓ <b>Опрос неявившихся «почему не пришёл»</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Время (день после форума): {t}")
    forum_date_set = await forum_date_for(code) is not None
    if not forum_date_set:
        lines.append("\n⚠️ «🗓 Дата начала форума» не задана — опрос не поставится, даже если Вкл здесь.")

    from cities import cities_module_on as _cmo, city_scope as _cscope
    scope = _cscope(code) if code and await _cmo() else None
    summary = await fnsp.summary_text(city_scope=scope)
    lines.append(f"\n{summary}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"forumnoshowpoll_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🕕 Время: {t}",
            callback_data=f"forumnoshowpoll_time:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumnoshowpoll_cfg:"))
async def forumnoshowpoll_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _noshow_poll_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumnoshowpoll_toggle:"))
async def forumnoshowpoll_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "forum_noshow_poll_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    await _safe_reschedule_noshow_poll(code)
    text, kb = await _noshow_poll_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("forumnoshowpoll_time:"))
async def forumnoshowpoll_time_start(callback: types.CallbackQuery, state: FSMContext):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.update_data(forumnoshowpoll_time_city=code)
    await state.set_state(ForumNoshowPollTimeEdit.waiting_value)
    await callback.message.answer(
        "Во сколько НА СЛЕДУЮЩИЙ ДЕНЬ после последнего дня форума слать опрос (московское "
        "время)? Формат <code>ЧЧ:ММ</code>, например <code>12:00</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(ForumNoshowPollTimeEdit), Command("cancel"))
@router.message(StateFilter(ForumNoshowPollTimeEdit), F.text == "Отмена")
async def cancel_forumnoshowpoll_time_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(ForumNoshowPollTimeEdit.waiting_value)
async def forumnoshowpoll_time_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("forumnoshowpoll_time_city")
    await state.set_state(None)

    if not await _city_allowed(message.from_user.id, code):
        await message.answer(_CITY_FORBIDDEN_ALERT, reply_markup=ReplyKeyboardRemove())
        return

    key = "forum_noshow_poll_time"
    value, error = validate_setting_value(key, (message.text or "").strip())
    if error:
        await message.answer(error, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(message.from_user.id, composed, value)
    else:
        await set_setting_by_admin(message.from_user.id, key, value)
    await _safe_reschedule_noshow_poll(code)

    text, kb = await _noshow_poll_cfg_text_kb(code)
    await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Трек «региональные форумы → Москва»: перенос неявившихся ────────────────────────────────
# Форма — тот же приём, что «Опрос неявившихся» выше (тумблер + время), плюс два кнопочных
# выбора без свободного ввода (город назначения — из `cities.enabled_cities()`, статус после
# переноса — тумблер-цикл между двумя значениями): «бот для людей» — код города/варианта
# менеджер никогда не печатает.

_RGNM_STATUS_LABELS = {
    rgnm.STATUS_MODE_KEEP: "Сохранить одобрение",
    rgnm.STATUS_MODE_TO_MODERATION: "Вернуть на модерацию",
}


async def _regional_noshow_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_setting_typed_for_city("regional_noshow_offer_enabled", code)
    t = await get_setting_typed_for_city("regional_noshow_offer_time", code) or "12:00"
    label = await city_label(code) if code else None
    on = enabled == "on"

    target_code = await rgnm.target_city_for(code)
    target_label = await city_label(target_code)
    status_mode = await rgnm.move_status_for(code)
    status_label = _RGNM_STATUS_LABELS[status_mode]

    lines = ["🚌 <b>Перенос неявившихся на форум в Москве</b>" + (f" — {html.escape(label)}" if label else "")]
    lines.append(f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}")
    lines.append(f"Время (день после форума): {t}")
    lines.append(f"Город назначения: {html.escape(target_label)}")
    lines.append(f"Статус после переноса: {status_label}")
    forum_date_set = await forum_date_for(code) is not None
    if not forum_date_set:
        lines.append("\n⚠️ «🗓 Дата начала форума» не задана — предложение не поставится, даже если Вкл здесь.")
    text_set = bool((await get_setting_typed_for_city("regional_noshow_offer_text", code) or "").strip())
    if not text_set:
        lines.append("\n⚠️ Текст предложения пуст — рассылка НЕ уйдёт, даже если включена здесь.")

    from cities import cities_module_on as _cmo, city_scope as _cscope
    scope = _cscope(code) if code and await _cmo() else None
    lines.append(f"\n{await rgnm.summary_text(city_scope=scope)}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Рассылка: {'✅ Вкл' if on else '❌ Выкл'}",
            callback_data=f"rgnm_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🕕 Время: {t}",
            callback_data=f"rgnm_time:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"🏙 Город назначения: {target_label}",
            callback_data=f"rgnm_target_start:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(
            text=f"Статус после переноса: {status_label}",
            callback_data=f"rgnm_status_toggle:{_encode_city(code)}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("rgnm_cfg:"))
async def rgnm_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _regional_noshow_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("rgnm_toggle:"))
async def rgnm_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "regional_noshow_offer_enabled"
    current = await get_setting_typed_for_city(key, code)
    new_val = "off" if current == "on" else "on"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _regional_noshow_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Вкл" if new_val == "on" else "❌ Выкл", show_alert=True)


@router.callback_query(F.data.startswith("rgnm_time:"))
async def rgnm_time_start(callback: types.CallbackQuery, state: FSMContext):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.update_data(rgnm_time_city=code)
    await state.set_state(RegionalNoshowMoveTimeEdit.waiting_value)
    await callback.message.answer(
        "Во сколько НА СЛЕДУЮЩИЙ ДЕНЬ после последнего дня форума слать предложение переноса "
        "(московское время)? Формат <code>ЧЧ:ММ</code>, например <code>12:00</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(RegionalNoshowMoveTimeEdit), Command("cancel"))
@router.message(StateFilter(RegionalNoshowMoveTimeEdit), F.text == "Отмена")
async def cancel_rgnm_time_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(RegionalNoshowMoveTimeEdit.waiting_value)
async def rgnm_time_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("rgnm_time_city")
    await state.set_state(None)

    if not await _city_allowed(message.from_user.id, code):
        await message.answer(_CITY_FORBIDDEN_ALERT, reply_markup=ReplyKeyboardRemove())
        return

    key = "regional_noshow_offer_time"
    value, error = validate_setting_value(key, (message.text or "").strip())
    if error:
        await message.answer(error, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        return

    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(message.from_user.id, composed, value)
    else:
        await set_setting_by_admin(message.from_user.id, key, value)

    text, kb = await _regional_noshow_cfg_text_kb(code)
    await message.answer("✅ Сохранено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("rgnm_status_toggle:"))
async def rgnm_status_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    key = "regional_noshow_move_status"
    current = await rgnm.move_status_for(code)
    new_val = (
        rgnm.STATUS_MODE_KEEP if current == rgnm.STATUS_MODE_TO_MODERATION
        else rgnm.STATUS_MODE_TO_MODERATION
    )
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    text, kb = await _regional_noshow_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(_RGNM_STATUS_LABELS[new_val], show_alert=True)


@router.callback_query(F.data.startswith("rgnm_target_start:"))
async def rgnm_target_start(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    cities = await enabled_cities()
    buttons = [
        [InlineKeyboardButton(
            text=c["label"], callback_data=f"rgnm_target_pick:{_encode_city(code)}:{c['code']}",
        )]
        for c in cities
    ]
    if not buttons:
        await callback.answer("Нет включённых городов.", show_alert=True)
        return
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"rgnm_cfg:{_encode_city(code)}")])
    await callback.message.answer(
        "🏙 <b>Куда переносить неявившихся?</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rgnm_target_pick:"))
async def rgnm_target_pick(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    code = _decode_city(parts[1])
    target_code = parts[2]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    from cities import get_city
    if get_city(target_code) is None:
        await callback.answer("Такого города нет.", show_alert=True)
        return

    key = "regional_noshow_target_city"
    if code and await cities_module_on():
        composed = per_city_key(key, code)
        await set_setting_by_admin(callback.from_user.id, composed, target_code)
    else:
        await set_setting_by_admin(callback.from_user.id, key, target_code)

    text, kb = await _regional_noshow_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Сохранено")
