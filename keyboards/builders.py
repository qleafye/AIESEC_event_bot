from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from config import config

# --- Main Menu ---
def get_main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔗 Моя реферальная ссылка")
    kb.button(text="👥 Мои приглашённые")
    kb.button(text="ℹ️ Информация о форуме")
    kb.button(text="📅 Программа форума")
    kb.button(text="🗣 Спикеры")
    kb.button(text="📞 Контакты")
    kb.button(text="❓ Задать вопрос")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True)

# --- Registration Keyboards ---

def get_yes_no_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Да")
    kb.button(text="Нет")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_source_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    items = [
        "От амбассадора", 
        "От друга", 
        "В ВК", 
        "В ТГ", 
        "В соцсетях партнера форума",
        "Увидел плакат в вузе",
        "Другое"
    ]
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
    kb.button(text="Всё верно ✓")
    kb.button(text="Изменить")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_skip_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Пропустить")
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)
