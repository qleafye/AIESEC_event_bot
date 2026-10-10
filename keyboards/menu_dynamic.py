"""Фильтр кнопок главного меню делегата: подпись — настройка (данные и правила —
`services/menu_labels.py`, без aiogram).

`MenuButton("menu_x")` узнаёт нажатие по подписи, актуальной для ГОРОДА делегата (своя у
города, общая, стандартная, прежняя). В состояниях делегата (анкета, вопрос организаторам,
сдача задания… — `handlers.states.DELEGATE_STATE_GROUPS`) меню не матчится: введённый там
текст — ответ, а не нажатие. Исключение — «ответы делегата» вне анкеты (вопрос, задание…):
там нажатие снимает состояние раньше фильтра (`handlers/menu_tap_escape.py`). Все фильтры меню одного апдейта делят один результат
(`_UPDATE_CACHE`): город делегата и подписи читаются один раз на сообщение.
"""
import contextvars
import logging
import re

from aiogram.filters import BaseFilter
from aiogram.types import Message

from cities import cities_module_on, default_city_code, normalize_city
from database.db import get_user
from services import menu_labels
from services.menu_labels import (  # noqa: F401 — реэкспорт для прежних импортов
    CONFERENCE_MENU_LABELS,
    LEGACY_MENU_TEXTS,
    MENU_LABEL_KEYS,
    MENU_LABEL_SETTING_KEYS,
    STATIC_MENU_TEXTS,
    caption_for,
    default_caption,
)
from domain.settings.schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# Прежнее имя (до того, как настраиваемыми стали все кнопки).
DYNAMIC_MENU_LABEL_KEYS = MENU_LABEL_KEYS
# (ключ, подпись, подсказка) — строки экрана «✏️ Подписи кнопок меню» в чате, в порядке меню.
MENU_LABEL_FIELDS: list[tuple[str, str, str]] = [
    (k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in MENU_LABEL_KEYS.values()
]

# Группы состояний делегата — строками, как в handlers/states.py (импорт handlers отсюда дал
# бы цикл; совпадение со states.DELEGATE_STATE_GROUPS сторожит тест).
DELEGATE_STATE_GROUPS: frozenset[str] = frozenset({
    "Registration", "_CompositeChat", "_LookupChat", "OnsiteReg",
    "Question", "GameSubmit", "SosReport", "SessionFeedbackComment", "ForumNoshowPollOther",
})

_LATIN = re.compile(r"[A-Za-z]")

# (сообщение, ключ кнопки) последнего апдейта: фильтры меню одного апдейта идут в одном
# asyncio-контексте. Сверка по `is` — другое сообщение = новый разбор.
_UPDATE_CACHE: contextvars.ContextVar[tuple[object, str | None] | None] = \
    contextvars.ContextVar("menu_label_update_cache", default=None)


async def _delegate_scope(user_id: int | None) -> tuple[str | None, bool]:
    """(город для подписей, английский ли делегат). Город — как в get_main_menu_kb: свой,
    иначе город по умолчанию; модуль городов выключен — только общие подписи."""
    user = None
    if user_id is not None:
        try:
            user = await get_user(user_id)
        except Exception as e:
            logger.warning("меню: делегат %s не прочитан: %s", user_id, e)
    city = None
    try:
        if await cities_module_on():
            city = normalize_city(user.get("event_city") if user else None) or default_city_code()
    except Exception as e:
        logger.warning("меню: город делегата %s не определён: %s", user_id, e)
    return city, bool(user and user.get("lang") == "en")


async def menu_key_for_text(text: str | None, message: object | None = None) -> str | None:
    """Какая кнопка меню подписана этим текстом для отправителя `message`? None — не кнопка.
    Fail-soft: сбой чтения = узнаём только стандартные подписи."""
    if not text:
        return None
    cached = _UPDATE_CACHE.get() if message is not None else None
    if cached is not None and cached[0] is message:
        return cached[1]
    try:
        data = await menu_labels.label_data()
    except Exception as e:
        logger.warning("подписи кнопок меню не прочитаны: %s", e)
        return menu_labels.STATIC_TEXT_TO_KEY.get(text)
    result = None
    # Быстрый отказ без чтения делегата: текст не похож ни на одну подпись, и английских
    # вариантов у него быть не может (нет латиницы).
    if text in menu_labels.all_texts(data) or _LATIN.search(text):
        from_user = getattr(message, "from_user", None)
        city, is_en = await _delegate_scope(getattr(from_user, "id", None))
        en_map = await menu_labels.manual_en_map() if is_en else None
        result = menu_labels.resolve(data, text, city, en_map)
    if message is not None:
        _UPDATE_CACHE.set((message, result))
    return result


async def all_dynamic_captions(menu_key: str | None = None) -> frozenset[str]:
    """Все подписи кнопки (или любой кнопки меню): любой город, прежние, стандартные."""
    try:
        return menu_labels.all_texts(await menu_labels.label_data(), menu_key)
    except Exception as e:
        logger.warning("подписи кнопок меню не прочитаны: %s", e)
        keys = [menu_key] if menu_key else list(MENU_LABEL_KEYS)
        return frozenset().union(*(STATIC_MENU_TEXTS[k] for k in keys))


async def is_dynamic_menu_text(text: str) -> bool:
    """Это подпись кнопки меню (любой кнопки, города)? Сторож ввода админки. Fail-soft: False."""
    if not text:
        return False
    try:
        return text in await all_dynamic_captions()
    except Exception as e:
        logger.warning("is_dynamic_menu_text не отработал: %s", e)
        return False


class MenuButton(BaseFilter):
    """Сообщение — нажатие кнопки меню `menu_key` под её актуальной подписью."""

    def __init__(self, menu_key: str):
        if menu_key not in MENU_LABEL_KEYS:
            raise KeyError(f"неизвестная кнопка меню: {menu_key}")
        self.menu_key = menu_key

    async def __call__(self, message: Message, raw_state: str | None = None) -> bool:
        text = getattr(message, "text", None)
        if not text:
            return False
        if raw_state and raw_state.split(":", 1)[0] in DELEGATE_STATE_GROUPS:
            return False
        try:
            return await menu_key_for_text(text, message) == self.menu_key
        except Exception as e:
            logger.warning("MenuButton(%s) не отработал: %s", self.menu_key, e)
            return False


# Прежнее имя фильтра (запись на сессии, тест).
DynamicMenuText = MenuButton
