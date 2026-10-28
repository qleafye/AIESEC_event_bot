"""Оформление карточки QR из админки (раздел «🎨 Оформление» → «🎟 Карточка QR»).

Главная страховка: пока менеджер ничего не настроил, карточка делегата байт-в-байт та же, что
была до появления оформления — утренняя рассылка QR не должна поменяться «сама». Отпечатки ниже
сняты с карточки ДО правки `services/forum/qr_card.py`; они зависят от версии Pillow (сжатие и
квантование PNG), поэтому сверяются только на той же версии, а независимо от версии сравниваются
варианты рендера в одном прогоне (без темы / theme=None / тема по умолчанию / тема из пустой БД)."""
from __future__ import annotations

import hashlib

import PIL
import pytest

from services.forum.qr_card import render_qr_card

# Сняты на Pillow 11.3.0 + segno 1.6.6 до добавления темы.
_GOLDEN_PIL_VERSION = "11.3.0"
_GOLDEN_SHA256_SHORT = "88eb8566e9dc205c71e678d888f3766560cbbb2387436b1dd32a8b8e0a32793f"
_GOLDEN_SHA256_LONG_NO_CITY = "ff43806d9e93854f1a39c366569fe5857b5add838eed1657abbb6d2e563ce9d0"

_SHORT = (
    "YL26·Иванова Анна·Москва·" + "x" * 22, "Иванова Анна", "Москва",
    "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
)
_LONG_NO_CITY = (
    "YL26·Константинопольская Александра Сергеевна·—·" + "y" * 22,
    "Константинопольская Александра Сергеевна Длиннофамильная", "",
    "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
)


def _sha(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


def test_default_render_is_byte_identical_to_pre_theme_card():
    if PIL.__version__ != _GOLDEN_PIL_VERSION:
        pytest.skip(f"отпечаток снят на Pillow {_GOLDEN_PIL_VERSION}, здесь {PIL.__version__}")
    assert _sha(render_qr_card(*_SHORT)) == _GOLDEN_SHA256_SHORT
    assert _sha(render_qr_card(*_LONG_NO_CITY)) == _GOLDEN_SHA256_LONG_NO_CITY


# ── тема карточки: реестр, загрузка, рендер ────────────────────────────────────────────

import asyncio  # noqa: E402
import io  # noqa: E402

from PIL import Image  # noqa: E402

from database import db  # noqa: E402
from domain.settings.schema import SETTINGS_SCHEMA  # noqa: E402
from services.forum import checkin as checkin_mod  # noqa: E402
from services.forum import qr_card_theme as theme_mod  # noqa: E402
from services.forum.qr_card_theme import (  # noqa: E402
    CARD_THEME_KEYS, DEFAULT_THEME, PALETTE, CardTheme, load_card_theme,
)
from tests.test_checkin_qr_260923 import UID, _seed_user, _use_tmp_db  # noqa: E402


class _FakeBot:
    """Bot без сети: `download` пишет заранее заданные байты по file_id."""

    def __init__(self, files: dict[str, bytes] | None = None, fail: bool = False):
        self.files = files or {}
        self.fail = fail
        self.downloads: list[str] = []
        self.photos: list[dict] = []

    async def download(self, file_id, destination):
        self.downloads.append(file_id)
        if self.fail:
            raise RuntimeError("сеть недоступна")
        destination.write(self.files[file_id])

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append({"chat_id": chat_id, "photo": photo, **kwargs})


def _png(size, color, mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _rgb_at(png: bytes, xy) -> tuple[int, int, int]:
    return Image.open(io.BytesIO(png)).convert("RGB").getpixel(xy)


def _near(a, b, tol=24) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_schema_has_nine_event_wide_card_keys():
    assert set(CARD_THEME_KEYS.values()) == {
        "qr_card_bg_color", "qr_card_text_color", "qr_card_accent_color", "qr_card_bg_image",
        "qr_card_logo", "qr_card_show_watermark", "qr_card_show_logo", "qr_card_show_city",
        "qr_card_show_footer",
    }
    for key in CARD_THEME_KEYS.values():
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "miniapp" and not entry.get("per_city")
        assert entry["label"].startswith("🎟 ")
    for key in ("qr_card_bg_color", "qr_card_text_color", "qr_card_accent_color"):
        assert SETTINGS_SCHEMA[key]["options"] == list(PALETTE)
        assert set(SETTINGS_SCHEMA[key]["option_labels"]) == set(PALETTE)
    assert SETTINGS_SCHEMA["qr_card_bg_color"]["default"] == "blue"
    assert SETTINGS_SCHEMA["qr_card_text_color"]["default"] == "white"
    assert SETTINGS_SCHEMA["qr_card_accent_color"]["default"] == "orange"
    assert "PNG" in SETTINGS_SCHEMA["qr_card_logo"]["prompt"]


def test_default_theme_matches_hardcoded_card_colors():
    from services.forum import qr_card

    assert PALETTE["blue"] == qr_card._BLUE
    assert PALETTE["orange"] == qr_card._ORANGE
    assert PALETTE["white"] == qr_card._WHITE
    assert DEFAULT_THEME == CardTheme(bg=qr_card._BLUE, text=qr_card._WHITE, accent=qr_card._ORANGE)


def test_default_variants_render_same_bytes_in_one_run(tmp_path):
    """Независимо от версии Pillow: без темы / None / тема по умолчанию / тема из пустой БД."""
    _use_tmp_db(tmp_path)
    loaded = asyncio.run(load_card_theme())
    assert loaded == DEFAULT_THEME
    for args in (_SHORT, _LONG_NO_CITY):
        base = render_qr_card(*args)
        assert render_qr_card(*args, theme=None) == base
        assert render_qr_card(*args, theme=DEFAULT_THEME) == base
        assert render_qr_card(*args, theme=loaded) == base


def test_golden_still_holds_with_theme_param():
    if PIL.__version__ != _GOLDEN_PIL_VERSION:
        pytest.skip(f"отпечаток снят на Pillow {_GOLDEN_PIL_VERSION}, здесь {PIL.__version__}")
    assert _sha(render_qr_card(*_SHORT, theme=DEFAULT_THEME)) == _GOLDEN_SHA256_SHORT
    assert _sha(render_qr_card(*_LONG_NO_CITY, theme=DEFAULT_THEME)) == _GOLDEN_SHA256_LONG_NO_CITY


def test_bg_color_changes_background():
    png = render_qr_card(*_SHORT, theme=CardTheme(bg=PALETTE["purple"]))
    assert not _near(_rgb_at(png, (10, 1430)), PALETTE["blue"])
    assert _near(_rgb_at(png, (10, 1430)), PALETTE["purple"], tol=30)


def test_bg_image_covers_canvas_and_keeps_full_color():
    png = render_qr_card(*_SHORT, theme=CardTheme(bg_image=_png((200, 100), (220, 20, 20)), show_watermark=False))
    assert _near(_rgb_at(png, (5, 1435)), (220, 20, 20), tol=8)
    assert _near(_rgb_at(png, (1075, 5)), (220, 20, 20), tol=8)
    # с картинкой — без палитры из 16 цветов, иначе фото постеризуется
    assert Image.open(io.BytesIO(png)).mode == "RGB"


def test_broken_bg_image_falls_back_to_color():
    png = render_qr_card(*_SHORT, theme=CardTheme(bg=PALETTE["green"], bg_image=b"not an image"))
    assert _near(_rgb_at(png, (10, 1430)), PALETTE["green"], tol=30)


def test_custom_logo_drawn_as_is_and_hidden_logo_lifts_title():
    logo = _png((300, 100), (0, 200, 0, 255), mode="RGBA")
    png = render_qr_card(*_SHORT, theme=CardTheme(logo=logo, show_watermark=False))
    assert _near(_rgb_at(png, (_PAD_PX + 20, _PAD_PX + 30)), (0, 200, 0), tol=8)
    from services.forum.qr_card import _background

    _, y_with = _background("Пропуск на Юлид", "ЮЛИД", CardTheme(show_watermark=False))
    _, y_without = _background("Пропуск на Юлид", "ЮЛИД", CardTheme(show_logo=False, show_watermark=False))
    assert y_without < y_with


_PAD_PX = 72


def test_footer_off_removes_orange_chip():
    def orange_pixels(png):
        img = Image.open(io.BytesIO(png)).convert("RGB")
        band = img.crop((0, 1200, 1080, 1440))
        return sum(1 for px in band.getdata() if _near(px, PALETTE["orange"], tol=20))

    on = render_qr_card(*_SHORT, theme=CardTheme(show_watermark=False))
    off = render_qr_card(*_SHORT, theme=CardTheme(show_watermark=False, show_footer=False))
    assert orange_pixels(on) > 1000
    assert orange_pixels(off) == 0


def test_city_off_equals_empty_city():
    theme = CardTheme(show_city=False)
    a = render_qr_card(*_SHORT, theme=theme)
    args = list(_SHORT)
    args[2] = ""
    b = render_qr_card(*args, theme=theme)
    assert a == b


def test_load_card_theme_reads_db_and_overrides(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.set_setting("qr_card_bg_color", "purple"))
    asyncio.run(db.set_setting("qr_card_show_city", "off"))
    theme = asyncio.run(load_card_theme())
    assert theme.bg == PALETTE["purple"] and theme.show_city is False
    theme = asyncio.run(load_card_theme(overrides={"qr_card_bg_color": "green"}))
    assert theme.bg == PALETTE["green"]
    # None в черновике = «убрано» -> дефолт ключа, а не значение из БД
    theme = asyncio.run(load_card_theme(overrides={"qr_card_bg_color": None, "qr_card_show_city": None}))
    assert theme.bg == PALETTE["blue"] and theme.show_city is True
    # неизвестное имя палитры — дефолт
    theme = asyncio.run(load_card_theme(overrides={"qr_card_text_color": "rainbow"}))
    assert theme.text == PALETTE["white"]


def test_load_card_theme_downloads_once_and_survives_failure(tmp_path):
    _use_tmp_db(tmp_path)
    theme_mod._image_cache.clear()
    asyncio.run(db.set_setting("qr_card_bg_image", "FILE_BG"))
    asyncio.run(db.set_setting("qr_card_logo", "FILE_LOGO"))

    broken = _FakeBot(fail=True)
    theme = asyncio.run(load_card_theme(bot=broken))
    assert theme.bg_image is None and theme.logo is None

    bot = _FakeBot({"FILE_BG": b"bg", "FILE_LOGO": b"logo"})
    theme = asyncio.run(load_card_theme(bot=bot))
    assert theme.bg_image == b"bg" and theme.logo == b"logo"
    asyncio.run(load_card_theme(bot=bot))
    assert sorted(bot.downloads) == ["FILE_BG", "FILE_LOGO"]  # второй раз — из кэша
    theme_mod._image_cache.clear()


def test_payload_unchanged_by_card_theme(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    user = {"telegram_id": UID, "full_name": "Иванов Иван", "event_city": "msk"}
    before = asyncio.run(checkin_mod.build_checkin_payload(user))
    for key, value in (("qr_card_bg_color", "dark"), ("qr_card_show_city", "off"),
                       ("qr_card_show_footer", "off"), ("qr_card_show_logo", "off")):
        asyncio.run(db.set_setting(key, value))
    assert asyncio.run(checkin_mod.build_checkin_payload(user)) == before


def test_checkin_card_uses_theme_from_registry(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    asyncio.run(db.set_setting("qr_card_bg_color", "purple"))
    png, _ = asyncio.run(checkin_mod.build_checkin_qr({
        "telegram_id": UID, "full_name": "Иванов Иван", "event_city": "msk",
    }))
    assert _near(_rgb_at(png, (10, 1430)), PALETTE["purple"], tol=30)


def test_sample_preview_is_a_card(tmp_path):
    _use_tmp_db(tmp_path)
    png = asyncio.run(theme_mod.sample_preview(CardTheme(bg=PALETTE["teal"])))
    assert Image.open(io.BytesIO(png)).size == (1080, 1440)
