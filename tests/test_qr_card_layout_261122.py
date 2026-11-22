"""Макет карточки QR: тот же код на чужой картинке, отпечаток и откат на голый QR.

Декодера QR в окружении нет. Центры модулей сверяются с матрицей `segno`, в том числе
после JPEG. Золотые SHA генератора живут в `tests/test_qr_card_theme_261028.py`.
"""
from __future__ import annotations

import asyncio
import io

import PIL
import pytest
import segno
from PIL import Image

from database import db
from services.forum import checkin as checkin_mod
from services.forum.checkin import build_payload
from services.forum.qr_card import layout_qr_origin, render_qr_card, render_qr_on_layout
from tests.test_checkin_qr_260923 import UID, _seed_user, _use_tmp_db
from tests.test_qr_card_theme_261028 import (
    _GOLDEN_PIL_VERSION,
    _GOLDEN_SHA256_LONG_NO_CITY,
    _GOLDEN_SHA256_SHORT,
    _LONG_NO_CITY,
    _SHORT,
    _sha,
)

_PAYLOAD = build_payload("YL26", "Иванова Анна", "Москва", "tok-layout")
_QR = {"x": 0.42, "y": 0.02, "w": 0.56, "h": 0.96}
_NAME = {"x": 0.02, "y": 0.2, "w": 0.34, "h": 0.35}


def _red_png(width: int = 1000, height: int = 500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _matrix(payload: str) -> list[list[bool]]:
    core = [[bool(cell) for cell in row] for row in segno.make(payload, error="m").matrix]
    border = 4
    size = len(core) + 2 * border
    grid = [[False] * size for _ in range(size)]
    for i, row in enumerate(core):
        for j, dark in enumerate(row):
            grid[i + border][j + border] = dark
    return grid


def _jpeg(png: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return Image.open(buf).convert("RGB")


def _ink(pixel) -> bool:
    r, g, b = pixel
    return r < 80 and g < 80 and b < 80


def _orange(pixel) -> bool:
    r, g, b = pixel
    return abs(r - 244) < 30 and abs(g - 137) < 30 and abs(b - 36) < 30


def test_layout_modules_match_payload_after_jpeg():
    png = render_qr_on_layout(_red_png(), {"qr": _QR}, _PAYLOAD, "Иванова Анна", "Москва")
    img = _jpeg(png)
    width, height = img.size
    left, top, scale, modules = layout_qr_origin(width, height, _QR, _PAYLOAD)
    assert scale >= 2
    step_x = int((4.5 + 1) * scale) - int(4.5 * scale)
    step_y = int((4.5 + 1) * scale) - int(4.5 * scale)
    assert step_x == step_y == scale
    grid = _matrix(_PAYLOAD)
    assert len(grid) == modules
    for i, row in enumerate(grid):
        for j, dark in enumerate(row):
            x = left + int((j + 0.5) * scale)
            y = top + int((i + 0.5) * scale)
            lum = sum(img.getpixel((x, y))) / 3
            assert (lum < 128) is dark


def test_name_frame_draws_ink_and_generator_chip_stays_off():
    bare = Image.open(io.BytesIO(render_qr_on_layout(
        _red_png(), {"qr": _QR}, _PAYLOAD, "Иванова Анна", "Москва",
    ))).convert("RGB")
    width, height = bare.size
    left, top, scale, modules = layout_qr_origin(width, height, _QR, _PAYLOAD)
    qr_box = (left, top, left + scale * modules, top + scale * modules)
    assert not any(
        _ink(bare.getpixel((x, y)))
        for y in range(height) for x in range(width)
        if not (qr_box[0] <= x < qr_box[2] and qr_box[1] <= y < qr_box[3])
    )
    named = Image.open(io.BytesIO(render_qr_on_layout(
        _red_png(), {"qr": _QR, "name": _NAME}, _PAYLOAD, "Иванова Анна", "Москва",
    ))).convert("RGB")
    nx = int((_NAME["x"] + _NAME["w"] / 2) * width)
    assert any(_ink(named.getpixel((x, ny))) for ny in range(height) for x in range(nx - 40, nx + 40))
    assert not any(_orange(bare.getpixel((x, y))) for y in range(0, height, 3) for x in range(0, width, 3))


def test_long_side_keeps_file_aspect():
    img = Image.open(io.BytesIO(render_qr_on_layout(
        _red_png(1000, 500), {"qr": _QR}, _PAYLOAD, "Анна", "Москва",
    )))
    assert max(img.size) == 1440
    assert img.size[0] / img.size[1] == pytest.approx(1000 / 500)


def test_generator_sha_unchanged_without_layout():
    if PIL.__version__ != _GOLDEN_PIL_VERSION:
        pytest.skip(f"отпечаток снят на Pillow {_GOLDEN_PIL_VERSION}, здесь {PIL.__version__}")
    assert _sha(render_qr_card(*_SHORT)) == _GOLDEN_SHA256_SHORT
    assert _sha(render_qr_card(*_LONG_NO_CITY)) == _GOLDEN_SHA256_LONG_NO_CITY


def _user() -> dict:
    return {"telegram_id": UID, "full_name": "Иванов Иван", "event_city": "msk"}


def test_fingerprint_follows_layout_not_theme_color(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    user = _user()
    before = asyncio.run(checkin_mod.card_fingerprint(user))
    asyncio.run(db.qr_card_layout_put("FILE-LAYOUT", {"qr": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}}))
    with_layout = asyncio.run(checkin_mod.card_fingerprint(user))
    assert with_layout and with_layout != before
    asyncio.run(db.set_setting("qr_card_bg_color", "purple"))
    assert asyncio.run(checkin_mod.card_fingerprint(user)) == with_layout
    asyncio.run(db.qr_card_layout_delete())
    assert asyncio.run(checkin_mod.card_fingerprint(user)) != with_layout
    assert asyncio.run(db.get_setting("qr_card_bg_color")) == "purple"


def test_broken_layout_is_plain_qr_and_not_cached(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    asyncio.run(db.qr_card_layout_put("FILE-LAYOUT", {"qr": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}}))

    def boom(*_args, **_kwargs):
        raise OSError("битый макет")

    async def fake_bytes(_bot, _file_id):
        return _red_png()

    monkeypatch.setattr("services.forum.qr_card.render_qr_on_layout", boom)
    monkeypatch.setattr("services.forum.qr_card_theme._image_bytes", fake_bytes)
    media, _caption, fingerprint = asyncio.run(checkin_mod.cached_checkin_photo(_user()))
    width, height = Image.open(io.BytesIO(media)).size
    assert width == height and width != 1080
    assert fingerprint is None
    assert asyncio.run(db.checkin_qr_card_get(UID)) is None
