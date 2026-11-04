"""Оформление карточки QR делегата под событие — из реестра, а не из кода `qr_card.py`.

Менеджер в «🎨 Оформление» → «🎟 Карточка QR» выбирает кнопками цвет фона (или картинку), цвет
заголовка, цвет плашки-подсказки, своё лого и какие элементы показывать (ключи `qr_card_*` в
`domain/settings/schema.py`). Здесь эти ключи превращаются в `CardTheme`, которую понимает
`render_qr_card`. Без единого заданного ключа получается `DEFAULT_THEME` — карточка байт-в-байт
прежняя. Содержимое QR тема не трогает: строку по-прежнему собирает `checkin.build_payload`.

Модуль без aiogram: Bot берётся параметром или лениво из `services.scheduler.get_bot()`, импорт
`checkin` — тоже ленивый (checkin сам лениво импортирует этот модуль)."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from services.forum.qr_card import _BLUE, _ORANGE, _WHITE

logger = logging.getLogger(__name__)

# Коды и подписи — в реестре (`_QR_CARD_PALETTE_LABELS`), здесь только цвет. Человеку HEX не
# показывается нигде — только подписи кнопок.
PALETTE: dict[str, tuple[int, int, int]] = {
    "blue": (0x03, 0x7E, 0xF3),
    "orange": (0xF4, 0x89, 0x24),
    "red": (0xF8, 0x5A, 0x40),
    "yellow": (0xFF, 0xC8, 0x45),
    "teal": (0x0C, 0xB9, 0xC1),
    "green": (0x00, 0xC1, 0x6E),
    "purple": (0x75, 0x52, 0xCC),
    "dark": (0x1C, 0x1C, 0x1C),
    "white": (0xFF, 0xFF, 0xFF),
}

# Поле темы -> ключ реестра. Порядок = порядок строк на экране админки.
CARD_THEME_KEYS: dict[str, str] = {
    "bg": "qr_card_bg_color",
    "bg_image": "qr_card_bg_image",
    "text": "qr_card_text_color",
    "accent": "qr_card_accent_color",
    "logo": "qr_card_logo",
    "show_watermark": "qr_card_show_watermark",
    "show_logo": "qr_card_show_logo",
    "show_city": "qr_card_show_city",
    "show_footer": "qr_card_show_footer",
}

_COLOR_FIELDS = ("bg", "text", "accent")
_IMAGE_FIELDS = ("bg_image", "logo")
_FLAG_FIELDS = ("show_watermark", "show_logo", "show_city", "show_footer")


@dataclass(frozen=True)
class CardTheme:
    """Неизменяемая — ключ `lru_cache` фона в `qr_card._background`."""

    bg: tuple[int, int, int] = _BLUE
    text: tuple[int, int, int] = _WHITE
    accent: tuple[int, int, int] = _ORANGE
    bg_image: bytes | None = None
    logo: bytes | None = None
    show_watermark: bool = True
    show_logo: bool = True
    show_city: bool = True
    show_footer: bool = True


DEFAULT_THEME = CardTheme()

# Скачанные картинки по file_id: утренняя рассылка на всю базу не качает фон на каждого
# (тот же приём, что `_template_cache` у сертификатов).
_image_cache: dict[str, bytes] = {}


def hex_of(name: str) -> str:
    """`#RRGGBB` цвета палитры — для проверки контраста (`shared.web_theme.contrast_ratio`)."""
    r, g, b = PALETTE.get(name, PALETTE["blue"])
    return f"#{r:02X}{g:02X}{b:02X}"


def _default_of(key: str):
    from domain.settings.schema import SETTINGS_SCHEMA

    return SETTINGS_SCHEMA[key].get("default")


async def _image_bytes(bot, file_id: str) -> bytes:
    cached = _image_cache.get(file_id)
    if cached is None:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        cached = buf.getvalue()
        _image_cache[file_id] = cached
    return cached


async def resolve_values(overrides: dict | None = None) -> dict[str, str | None]:
    """Значения ключей `qr_card_*` (ключ реестра -> значение): из БД, поверх — черновик
    `overrides` (`None` в черновике = «убрано», т.е. дефолт ключа)."""
    from domain.settings.schema import get_setting_typed

    overrides = overrides or {}
    values: dict[str, str | None] = {}
    for key in CARD_THEME_KEYS.values():
        if key in overrides:
            value = overrides[key]
            values[key] = _default_of(key) if value is None else value
        else:
            values[key] = await get_setting_typed(key)
    return values


async def load_card_theme(overrides: dict | None = None, bot=None) -> CardTheme:
    """Тема карточки из реестра (+ черновик админки `overrides`). Никогда не бросает из-за
    картинок: не скачалась — фон цветом / встроенное лого и warning в лог."""
    values = await resolve_values(overrides)
    fields: dict = {}
    for field in _COLOR_FIELDS:
        key = CARD_THEME_KEYS[field]
        name = values[key]
        if name not in PALETTE:
            name = _default_of(key)
        fields[field] = PALETTE[name]
    for field in _FLAG_FIELDS:
        fields[field] = values[CARD_THEME_KEYS[field]] != "off"
    for field in _IMAGE_FIELDS:
        file_id = values[CARD_THEME_KEYS[field]]
        if not file_id:
            continue
        try:
            if bot is None:
                from services.scheduler import get_bot

                bot = get_bot()
            fields[field] = await _image_bytes(bot, file_id)
        except Exception as e:  # оформление не важнее пропуска
            logger.warning(f"load_card_theme: картинка {field} не скачалась, без неё: {e}")
    return CardTheme(**fields)


SAMPLE_NAME = "Иванова Анна"
SAMPLE_CITY = "Москва"


async def sample_preview(theme: CardTheme) -> bytes:
    """Превью карточки для админки: вымышленный делегат и ненастоящий TEST-токен (как у
    `build_test_qr`) — реальных данных делегатов в превью нет."""
    import asyncio
    from functools import partial

    from services.forum.checkin import _TEST_TOKEN_PREFIX, _event_tag, build_payload, card_texts
    from services.forum.qr_card import render_qr_card

    title, footer, watermark = await card_texts()
    payload = build_payload(await _event_tag(), SAMPLE_NAME, SAMPLE_CITY, _TEST_TOKEN_PREFIX + "PREVIEW")
    return await asyncio.to_thread(
        partial(render_qr_card, payload, SAMPLE_NAME, SAMPLE_CITY, title, footer, watermark, theme=theme),
    )
