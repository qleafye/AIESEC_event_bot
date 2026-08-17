import logging
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from config import config
from database.db import get_user
from settings_schema import get_setting_typed
from cities import get_setting_typed_for_city, cities_module_on, normalize_city

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
    ("menu_coins", "🪙 Мои монеты"),
    ("menu_game_tasks", "🎯 Задания"),
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

    kb = ReplyKeyboardBuilder()
    for key, text in MENU_BUTTONS:
        # menu_* is a registry `enum` key (options ["on","off"], default "on") -- the enum
        # branch of `_parse_setting` is `raw if raw else default`, so an unset/empty stored
        # value resolves to "on" exactly like the old `val is None or val == "on"` idiom;
        # any other stored value (including junk) resolves to itself and fails the `== "on"`
        # check, hiding the button exactly as before.
        val = await get_setting_typed_for_city(key, code)
        if val == "on":
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
    kb.button(text="Да")
    kb.button(text="Нет")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

DEFAULT_SOURCE_OPTIONS = [
    "Соцсети Юлид",
    "Соцсети АЙСЕК",
    "Университетские каналы",
    "Рассказал друг/знакомый",
    "Узнал от амбассадора",
    "Узнал от блогера",  # 2026-08-17: Instagram bloggers without direct reg link — track this channel
    "Другое",
]

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
    kb.button(text="Да, в ВУЗе или колледже")
    kb.button(text="Нет, завершил(а) обучение")
    kb.button(text="Нет, не получал(а) образование")
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
    for lc in ["EG", "SPUEF", "Moscow", "Tyumen", "Ufa", "Ekaterinburg"]:
        kb.button(text=lc)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


# --- Conference (RusCo) reg-flow keyboards ---

def get_department_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for d in ["OGV", "OGT", "MKT", "F&L", "BD", "LCP", "EwA"]:
        kb.button(text=d)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_aiesec_role_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for r in ["Member", "TL", "Manager", "VP", "LCP", "Coordinator"]:
        kb.button(text=r)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_english_level_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for lvl in ["Начальный", "Средний", "Продвинутый", "Свободный"]:
        kb.button(text=lvl)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_arrival_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for a in ["В дни конфы", "Заранее", "После"]:
        kb.button(text=a)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def get_housing_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for h in ["Хост", "Сам(а)", "Не нужно"]:
        kb.button(text=h)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_position_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for pos in ["AIESECer", "Alumni", "AIESEC Friend"]:
        kb.button(text=pos)
    kb.button(text="Другое")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_attendance_format_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Offline")
    kb.button(text="Online")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_informal_day_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Да")
    kb.button(text="Нет")
    kb.button(text="Буду только в онлайне")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_course_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for i in range(1, 5):
        kb.button(text=str(i))
    kb.button(text="5+")
    kb.button(text="Магистратура/Аспирантура")
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
