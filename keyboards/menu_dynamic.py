"""Подписи динамических кнопок главного меню (запись на сессии, тест компетенций).

В отличие от статичных `MENU_BUTTONS` подпись этих кнопок настраивается менеджером по городу
(`session_enroll_menu_label` / `quiz_menu_label`, per_city). Поэтому хендлер не может стоять
на `F.text.in_(...)` с константой: фильтр `DynamicMenuText` сравнивает сообщение с ТЕКУЩИМИ
подписями (общая, по каждому городу, дефолт и английские варианты). Дефолт принимается всегда —
у делегата может висеть старая клавиатура, пока менеджер переименовал кнопку.

Модуль не знает о гейтах видимости (тумблер, одобрение) — их делает `keyboards/builders`.
Импортов handlers нет: фильтр сравнивает текст со всеми подписями всех городов.
"""
import logging

from aiogram.filters import BaseFilter
from aiogram.types import Message

from cities import city_codes, get_setting_typed_for_city, per_city_key
from database.db import get_setting
from i18n_ui_en import MENU_EN
from services.i18n import load_map, tr
from settings_schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# Ключ кнопки меню -> ключ настройки с её подписью.
DYNAMIC_MENU_LABEL_KEYS: dict[str, str] = {
    "menu_session_enroll": "session_enroll_menu_label",
    "menu_quiz": "quiz_menu_label",
}


def _default_caption(label_key: str) -> str:
    return str(SETTINGS_SCHEMA[label_key].get("default") or "")


async def caption_for(menu_key: str, city: str | None, lang: str = "ru") -> str:
    """Текущая подпись кнопки для города: своя, иначе общая/дефолт; для EN — перевод
    (при его отсутствии — исходная подпись). Никогда не пустая строка."""
    label_key = DYNAMIC_MENU_LABEL_KEYS[menu_key]
    default = _default_caption(label_key)
    try:
        value = await get_setting_typed_for_city(label_key, city)
    except Exception as e:
        logger.warning("caption_for(%s, %s) не прочитана: %s", menu_key, city, e)
        value = None
    caption = str(value).strip() if value else ""
    caption = caption or default
    if lang == "en":
        tr_map = await load_map("en")
        translated = tr(caption, "en", tr_map) or caption
        if translated == caption:
            translated = MENU_EN.get(caption, caption)
        caption = translated
    return caption


async def all_dynamic_captions(menu_key: str | None = None) -> frozenset[str]:
    """Все подписи, под которыми кнопка может прийти от делегата: дефолт, общее значение,
    значения по каждому городу, их EN-переводы и EN-вариант дефолта из `MENU_EN`."""
    keys = [menu_key] if menu_key else list(DYNAMIC_MENU_LABEL_KEYS)
    found: set[str] = set()
    try:
        tr_map = await load_map("en")
    except Exception as e:
        logger.warning("карта EN-переводов не прочитана: %s", e)
        tr_map = {}
    for mk in keys:
        label_key = DYNAMIC_MENU_LABEL_KEYS[mk]
        raw_values = [_default_caption(label_key)]
        try:
            raw_values.append(await get_setting(label_key) or "")
            for code in city_codes():
                composed = per_city_key(label_key, code)
                if composed:
                    raw_values.append(await get_setting(composed) or "")
        except Exception as e:
            logger.warning("подписи %s не прочитаны: %s", label_key, e)
        for raw in raw_values:
            raw = (raw or "").strip()
            if not raw:
                continue
            found.add(raw)
            en = tr(raw, "en", tr_map)
            if en:
                found.add(en)
            if raw in MENU_EN:
                found.add(MENU_EN[raw])
    return frozenset(found)


async def is_dynamic_menu_text(text: str) -> bool:
    """Это подпись динамической кнопки меню (любого города/языка)? Fail-soft: False."""
    if not text:
        return False
    try:
        return text in await all_dynamic_captions()
    except Exception as e:
        logger.warning("is_dynamic_menu_text не отработал: %s", e)
        return False


class DynamicMenuText(BaseFilter):
    """Сообщение — тап по динамической кнопке `menu_key` (подпись текущая или дефолтная)."""

    def __init__(self, menu_key: str):
        self.menu_key = menu_key

    async def __call__(self, message: Message) -> bool:
        text = getattr(message, "text", None)
        if not text:
            return False
        try:
            return text in await all_dynamic_captions(self.menu_key)
        except Exception as e:
            logger.warning("DynamicMenuText(%s) не отработал: %s", self.menu_key, e)
            return False
