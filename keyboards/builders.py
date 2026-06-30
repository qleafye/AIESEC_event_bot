from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from config import config
from database.db import get_setting

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
]

async def get_main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for key, text in MENU_BUTTONS:
        val = await get_setting(key)
        if (val == "on") if val is not None else True:
            kb.button(text=text)
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
    "От амбассадора",
    "От друга",
    "В ВК",
    "В ТГ",
    "В соцсетях партнера форума",
    "Увидел плакат в вузе",
    "Другое",
]

async def get_source_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    custom = await get_setting("source_options")
    if custom:
        items = [line.strip() for line in custom.split("\n") if line.strip()]
    else:
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
    for d in ["OGV", "OGT", "MKT", "BD"]:
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

def get_socials_kb(tg_url: str, vk_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if tg_url:
        builder.button(text="Группа в Telegram", url=tg_url)
    if vk_url:
        builder.button(text="Группа во ВКонтакте", url=vk_url)
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
