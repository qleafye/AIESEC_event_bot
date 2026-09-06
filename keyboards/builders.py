import logging
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from config import config
from database.db import get_user, has_faq_for_city
from settings_schema import get_setting_typed
from cities import get_setting_typed_for_city, cities_module_on, normalize_city
# Phase 21 (21-01, FORM-SYNC-01): литеральные списки вариантов ответа живут в корневом
# aiogram-free reg_options.py — общая точка правды для бота (эти клавиатуры) и будущего
# Mini App (reg_engine.step_spec()). Сами клавиатуры (ReplyKeyboardBuilder, add_other/
# add_skip, порядок kb.adjust(...)) остаются здесь без изменений.
from reg_options import (
    DEFAULT_SOURCE_OPTIONS,
    EDUCATION_STATUS_OPTIONS,
    COURSE_OPTIONS,
    DEPARTMENT_OPTIONS,
    AIESEC_ROLE_OPTIONS,
    ENGLISH_LEVEL_OPTIONS,
    ARRIVAL_OPTIONS,
    HOUSING_OPTIONS,
    POSITION_OPTIONS,
    ATTENDANCE_FORMAT_OPTIONS,
    INFORMAL_DAY_OPTIONS,
    LOCAL_COMMITTEE_OPTIONS,
    YES_NO_OPTIONS,
)

logger = logging.getLogger(__name__)

# --- Main Menu ---

MENU_BUTTONS = [
    ("menu_referral", "🔗 Моя реферальная ссылка"),
    ("menu_invites", "👥 Мои приглашённые"),
    ("menu_info", "ℹ️ Информация о форуме"),
    ("menu_program", "📅 Программа форума"),
    ("menu_speakers", "🗣 Спикеры"),
    ("menu_contacts", "📞 Контакты"),
    ("menu_question", "❓ Задать вопрос"),
    # Quick 260906-8uq (FAQ-01..06): рядом с «Задать вопрос» — экран готовых ответов.
    # Дополнительный гейт ниже (`has_faq_for_city`) прячет кнопку, пока в FAQ нет ни одного
    # включённого пункта, — тот же приём, что у menu_miniapp (T-19-54).
    ("menu_faq", "❓ Частые вопросы"),
    ("menu_coins", "🪙 Мои монеты"),
    ("menu_game_tasks", "🎯 Задания"),
    # Phase 19 (D-10): текстовая reply-кнопка «📱 Приложение» — НЕ web_app-кнопка (Pitfall 1:
    # KeyboardButton(web_app=...) даёт simple web view без initData, делегат не
    # аутентифицируется). Хендлер `handlers/user_actions.py::open_miniapp_button` отвечает
    # сообщением с inline web_app-кнопкой; там initData полный.
    ("menu_miniapp", "📱 Приложение"),
    # Phase 27 (27-04, LANG-01): переключатель языка анкеты в главном меню. Default этого
    # ключа — "off" (единственное исключение из конвенции menu_* default "on", см.
    # settings_schema.py) И доп. гейт ниже (тот же приём, что у menu_miniapp/menu_faq):
    # кнопка не появится ни от одного включения по отдельности — нужны оба, менеджер
    # включает модуль («📝 Анкета» → «Английский язык анкеты») и саму кнопку («🔘 Кнопки
    # меню»), иначе тап по показанной, но мёртвой кнопке (модуль ещё выключен) был бы
    # нарушением «бот для людей».
    ("menu_lang", "🌐 Язык / Language"),
]

async def get_main_menu_kb(telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    # Phase 09.2 (B): resolve the delegate's city ONCE, before the button loop -- module off
    # (or no telegram_id, e.g. the two legacy tests that call get_main_menu_kb() bare) means
    # get_user is never even called, so there is not a single extra DB read over today.
    # A resolve failure (bad row, exception) must not break the menu -- buttons matter more
    # than the city, so it fails soft to code=None (global values), same idiom as
    # handlers/user_actions.py::show_game_tasks.
    code = None
    try:
        if telegram_id is not None and await cities_module_on():
            user = await get_user(telegram_id)
            code = normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"get_main_menu_kb: city resolve failed for {telegram_id}: {e}")
        code = None

    # Phase 19 (D-10, WR-05): miniapp_enabled resolved ONCE before the loop (same idiom as the
    # city code above) -- a single extra DB read per menu render, not one per button. Fail-soft:
    # a read failure means "no button", never a broken menu.
    miniapp_on = False
    try:
        miniapp_on = await get_setting_typed("miniapp_enabled") == "on"
    except Exception as e:
        logger.error(f"get_main_menu_kb: miniapp_enabled resolve failed: {e}")
        miniapp_on = False

    # Quick 260906-8uq (FAQ-01..06, T-19-54 idiom): one extra read before the loop -- fail-soft
    # (a read error means "no button", never a broken menu), same shape as miniapp_on above.
    faq_on = False
    try:
        faq_on = await has_faq_for_city(code)
    except Exception as e:
        logger.error(f"get_main_menu_kb: has_faq_for_city resolve failed for {telegram_id}: {e}")
        faq_on = False

    # Phase 27 (27-04, LANG-01): тот же идиом, что miniapp_on/faq_on выше — одно доп. чтение
    # ПЕРЕД циклом, не одно на кнопку. Fail-soft: сбой чтения значит «нет кнопки», меню цело.
    lang_module_on = False
    try:
        lang_module_on = await get_setting_typed("delegate_lang_enabled") == "on"
    except Exception as e:
        logger.error(f"get_main_menu_kb: delegate_lang_enabled resolve failed: {e}")
        lang_module_on = False

    kb = ReplyKeyboardBuilder()
    for key, text in MENU_BUTTONS:
        # menu_* is a registry `enum` key (options ["on","off"], default "on") -- the enum
        # branch of `_parse_setting` is `raw if raw else default`, so an unset/empty stored
        # value resolves to "on" exactly like the old `val is None or val == "on"` idiom;
        # any other stored value (including junk) resolves to itself and fails the `== "on"`
        # check, hiding the button exactly as before.
        val = await get_setting_typed_for_city(key, code)
        if val == "on":
            # Phase 19 (D-10, T-19-54): menu_miniapp needs TWO extra gates on top of the
            # ordinary menu toggle — the app itself must be enabled, and a public URL must be
            # configured (empty URL means no entry points exist at all, Pitfall 10). Every
            # other button is untouched by this branch.
            if key == "menu_miniapp" and not (miniapp_on and config.DASHBOARD_PUBLIC_URL):
                continue
            # Quick 260906-8uq: пустой FAQ (ни одного включённого пункта, ни общего, ни
            # своего города) — кнопки нет; появляется сама, как только менеджер завёл первый
            # пункт (has_faq_for_city).
            if key == "menu_faq" and not faq_on:
                continue
            # Phase 27 (27-04): вторая половина гейта — сама кнопка value=="on" (проверено
            # выше общей веткой `if val == "on"`) недостаточна, пока не включён модуль.
            if key == "menu_lang" and not lang_module_on:
                continue
            kb.button(text=text)
    # Persistent "upload receipt" entry — only while the user still owes one.
    # Lazy import avoids a circular import (payment imports get_main_menu_kb); fail-soft.
    if telegram_id is not None:
        try:
            from handlers.payment import should_offer_receipt_upload
            if await should_offer_receipt_upload(telegram_id):
                kb.button(text="💳 Оплата")
        except Exception:
            pass
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

# --- Registration Keyboards ---

def get_yes_no_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in YES_NO_OPTIONS:
        kb.button(text=opt)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

async def get_source_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    # REG-02: read through the registry accessor. The registry's list default is None
    # (not DEFAULT_SOURCE_OPTIONS), so the empty->DEFAULT_SOURCE_OPTIONS fallback guard is
    # kept here to preserve exact pre-migration behavior (T-06-11).
    items = await get_setting_typed("source_options")
    if not items:
        items = DEFAULT_SOURCE_OPTIONS
    for item in items:
        kb.button(text=item)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_education_status_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in EDUCATION_STATUS_OPTIONS:
        kb.button(text=opt)
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_universities_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for uni in config.UNIVERSITIES:
        kb.button(text=uni)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_local_committee_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for lc in LOCAL_COMMITTEE_OPTIONS:
        kb.button(text=lc)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


# --- Conference (RusCo) reg-flow keyboards ---

def get_department_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for d in DEPARTMENT_OPTIONS:
        kb.button(text=d)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_aiesec_role_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for r in AIESEC_ROLE_OPTIONS:
        kb.button(text=r)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_english_level_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for lvl in ENGLISH_LEVEL_OPTIONS:
        kb.button(text=lvl)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_arrival_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for a in ARRIVAL_OPTIONS:
        kb.button(text=a)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_housing_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for h in HOUSING_OPTIONS:
        kb.button(text=h)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_position_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for pos in POSITION_OPTIONS:
        kb.button(text=pos)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_attendance_format_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in ATTENDANCE_FORMAT_OPTIONS:
        kb.button(text=opt)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_informal_day_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in INFORMAL_DAY_OPTIONS:
        kb.button(text=opt)
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_course_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for opt in COURSE_OPTIONS:
        kb.button(text=opt)
    kb.adjust(3, 2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

# --- Info & Misc ---

def get_info_submenu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Дата и время", callback_data="info_date")
    builder.button(text="Место проведения", callback_data="info_place")
    builder.adjust(1)
    return builder.as_markup()

def _normalize_url(raw: str) -> str | None:
    """WR-04: admin contact settings are free text — a bare "@channel" or "vk.com/x" (no
    scheme) makes Telegram reject the whole message with BUTTON_URL_INVALID. Normalize to a
    valid URL; return None when there's nothing usable so the button is simply omitted."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    return f"https://{raw}"


def get_socials_kb(tg_url: str, vk_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    tg = _normalize_url(tg_url)
    vk = _normalize_url(vk_url)
    if tg:
        builder.button(text="Группа в Telegram", url=tg)
    if vk:
        builder.button(text="Группа во ВКонтакте", url=vk)
    return builder.as_markup()

def get_cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Отмена")
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_confirm_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Всё верно")
    kb.button(text="Изменить")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_phone_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="\U0001f4f1 Поделиться контактом", request_contact=True)
    kb.button(text="Пропустить")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_skip_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Пропустить")
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
