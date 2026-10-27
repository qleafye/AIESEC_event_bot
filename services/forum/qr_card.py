"""Оформленная карточка QR делегата: синий фон Юлида, белая карточка с кодом, имя и город.

Содержимое QR не трогаем — строку собирает `services.forum.checkin.build_payload`, здесь только
картинка вокруг неё. Рендер синхронный и тяжёлый (Pillow), вызывающий зовёт его через
`asyncio.to_thread` и при любой ошибке шлёт прежний голый QR — пропуск важнее оформления.

Цвета — из `refs/BRAND.md` (YouLead'26), шрифты — те же woff2 Mini App, что у учебного листа
(`checkin_training.sheet_font`), лого — `dashboard/static/brand/aisek-logo-ru.png`, перекрашенное
в белый (вариант «белый на синем» по BlueBook). Макета от дизайнеров нет — когда пришлют,
менять только этот модуль."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import segno

_ROOT = Path(__file__).resolve().parents[2]
_FONTS_DIR = _ROOT / "miniapp" / "static" / "fonts"
_LOGO = _ROOT / "dashboard" / "static" / "brand" / "aisek-logo-ru.png"

_W, _H = 1080, 1440
_BLUE = (3, 126, 243)
_ORANGE = (244, 137, 36)
_TEXT = (28, 28, 28)
_MUTED = (82, 86, 94)
_WHITE = (255, 255, 255)

_PAD = 72          # поле от краёв холста
_CARD_PAD = 56     # поле внутри белой карточки
_RADIUS = 48


def _font(name: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(_FONTS_DIR / name), size)


def _fit(draw, text: str, name: str, size: int, max_w: int, min_size: int = 34):
    """Самый крупный шрифт не меньше `min_size`, в который строка влезает по ширине."""
    while size > min_size:
        font = _font(name, size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= 4
    return _font(name, min_size)


def _wrap(draw, text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        probe = f"{cur} {word}".strip()
        if draw.textlength(probe, font=font) <= max_w or not cur:
            cur = probe
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _watermark(img, text: str) -> None:
    """Диагональные надписи названия поверх фона, прозрачность ~20% — паттерн из брендбука Юлида."""
    from PIL import Image, ImageDraw

    font = _font("raleway-800italic.woff2", 64)
    tile = Image.new("RGBA", (_W * 2, _H * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    step_x = int(draw.textlength(text + "   ", font=font))
    for row, y in enumerate(range(0, _H * 2, 120)):
        shift = (row % 2) * step_x // 2
        for x in range(-step_x, _W * 2, step_x):
            draw.text((x + shift, y), text, font=font, fill=(255, 255, 255, 22))
    tile = tile.rotate(18, resample=Image.BICUBIC)
    left, top = (tile.width - _W) // 2, (tile.height - _H) // 2
    img.alpha_composite(tile.crop((left, top, left + _W, top + _H)))


def _white_logo(height: int):
    from PIL import Image

    logo = Image.open(_LOGO).convert("RGBA")
    # Синие пиксели -> белые, белые буквы -> прозрачные: плашка «АЙСЕК» становится белой с
    # буквами цвета фона, человечки — белыми. Ровно так лого стоит на сертификатах форума.
    red, alpha = logo.getchannel("R"), logo.getchannel("A")
    keep = red.point(lambda r: 255 - max(0, r - 3) * 255 // 252)
    white = Image.new("RGBA", logo.size, _WHITE + (0,))
    white.putalpha(Image.composite(keep, alpha.point(lambda _: 0), alpha.point(lambda a: a)))
    ratio = height / white.height
    return white.resize((int(white.width * ratio), height), Image.LANCZOS)


@lru_cache(maxsize=4)
def _background(title: str, watermark: str):
    """Фон с паттерном, лого и заголовком — одинаковый у всех делегатов события, поэтому
    собирается один раз: утренняя рассылка QR идёт на всю базу, и поворот паттерна на каждого
    человека добавлял бы почти секунду. Возвращает `(картинка, y под заголовком)`."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (_W, _H), _BLUE + (255,))
    _watermark(img, watermark)
    draw = ImageDraw.Draw(img)
    logo = _white_logo(64)
    img.alpha_composite(logo, (_PAD, _PAD))
    y = _PAD + logo.height + 40
    f_title = _fit(draw, title, "raleway-800italic.woff2", 76, _W - 2 * _PAD, min_size=48)
    draw.text((_PAD, y), title, font=f_title, fill=_WHITE)
    return img, y + f_title.size + 44


def render_qr_card(payload: str, name: str, city: str, title: str, footer: str, watermark: str) -> bytes:
    """PNG 1080×1440: лого и заголовок на синем, белая карточка с QR, имя и город под кодом,
    оранжевая плашка с подсказкой внизу. Бросает ImportError без Pillow, OSError без шрифтов."""
    from PIL import Image, ImageDraw

    background, y = _background(title, watermark)
    img = background.copy()
    draw = ImageDraw.Draw(img)

    card_w = _W - 2 * _PAD
    qr_side = 660
    qr = segno.make(payload, error="m")
    modules = qr.symbol_size(scale=1, border=0)[0]
    scale = qr_side // modules
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=0, dark=_TEXT)
    qr_img = Image.open(buf).convert("RGBA")

    inner_w = card_w - 2 * _CARD_PAD
    f_name = _fit(draw, name, "raleway-800.woff2", 64, inner_w, min_size=40)
    name_lines = _wrap(draw, name, f_name, inner_w)
    f_city = _font("lato-700.woff2", 40)
    text_h = len(name_lines) * (f_name.size + 10) + (f_city.size + 14 if city else 0)

    card_h = _CARD_PAD + qr_img.height + 40 + text_h + _CARD_PAD
    card_top = y
    draw.rounded_rectangle(
        (_PAD, card_top, _PAD + card_w, card_top + card_h), radius=_RADIUS, fill=_WHITE,
    )
    img.alpha_composite(qr_img, (_PAD + (card_w - qr_img.width) // 2, card_top + _CARD_PAD))

    ty = card_top + _CARD_PAD + qr_img.height + 40
    for line in name_lines:
        draw.text((_W // 2, ty), line, font=f_name, fill=_TEXT, anchor="ma")
        ty += f_name.size + 10
    if city:
        ty += 4
        draw.text((_W // 2, ty), city, font=f_city, fill=_MUTED, anchor="ma")

    f_footer = _fit(draw, footer, "lato-700.woff2", 40, _W - 2 * _PAD - 80, min_size=28)
    chip_w = int(draw.textlength(footer, font=f_footer)) + 80
    chip_h = f_footer.size + 44
    chip_top = min(card_top + card_h + 48, _H - _PAD - chip_h)
    chip_left = (_W - chip_w) // 2
    draw.rounded_rectangle(
        (chip_left, chip_top, chip_left + chip_w, chip_top + chip_h), radius=chip_h // 2, fill=_ORANGE,
    )
    draw.text((_W // 2, chip_top + chip_h // 2), footer, font=f_footer, fill=_WHITE, anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
