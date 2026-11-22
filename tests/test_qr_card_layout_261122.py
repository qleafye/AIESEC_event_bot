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


def test_upload_rejects_pdf_and_keeps_layout_apart_from_colors(tmp_path):
    import re
    from pathlib import Path

    from fastapi import HTTPException

    from handlers.access.admin_caps import required_capability
    from handlers.settings import admin_qr_card_theme as screen
    from handlers.states import QrCardTheme
    from miniapp.deps import Principal
    from miniapp.routers import qr_card_layout as layout_api
    from tests.test_admin_sections_ia20 import FakeAnswerMessage, FakeDocument, FakeState
    from tests.test_qr_card_theme_261028 import _FakeBot, _cb
    from tests.test_roles_phase8 import _flat_callback_data, _roles_ready

    _roles_ready(tmp_path)
    state = FakeState(state=QrCardTheme.layout)

    pdf = FakeAnswerMessage(document=FakeDocument("PDF", "application/pdf"))
    pdf.document.file_name = "card.pdf"
    pdf.bot = _FakeBot()
    asyncio.run(screen.qrct_layout_file(pdf, state))
    assert asyncio.run(db.qr_card_layout_get()) is None
    assert "PNG или JPEG" in pdf.sent[-1][0]
    assert pdf.bot.downloads == []

    huge = FakeAnswerMessage(document=FakeDocument("BIG", "image/png"))
    huge.document.file_name = "big.png"
    huge.document.file_size = 11 * 1024 * 1024
    huge.bot = _FakeBot({"BIG": b"x" * 10})
    asyncio.run(screen.qrct_layout_file(huge, state))
    assert asyncio.run(db.qr_card_layout_get()) is None
    assert "10 МБ" in huge.sent[-1][0]
    assert huge.bot.downloads == []

    png = _red_png(80, 40)
    small = FakeAnswerMessage(document=FakeDocument("SMALL", "image/png"))
    small.document.file_name = "card.png"
    small.document.file_size = len(png)
    small.bot = _FakeBot({"SMALL": png})
    asyncio.run(screen.qrct_layout_file(small, state))
    row = asyncio.run(db.qr_card_layout_get())
    assert row["file_id"] == "SMALL"
    assert "qr" in row["markup"]
    assert "name" not in row["markup"]

    asyncio.run(db.set_setting("qr_card_bg_color", "purple"))
    asyncio.run(screen.qrct_reset_go(_cb("qrct_reset_go"), FakeState()))
    kept = asyncio.run(db.qr_card_layout_get())
    assert kept["file_id"] == "SMALL"
    assert asyncio.run(db.get_setting("qr_card_bg_color")) is None

    asyncio.run(db.set_setting("qr_card_bg_color", "teal"))
    asyncio.run(screen.qrct_layout_rm_go(_cb("qrct_layout_rm_go"), FakeState()))
    assert asyncio.run(db.qr_card_layout_get()) is None
    assert asyncio.run(db.get_setting("qr_card_bg_color")) == "teal"

    text = asyncio.run(screen.render_qr_card_theme_text({}))
    kb = asyncio.run(screen.build_qr_card_theme_keyboard({}))
    labels = " ".join(btn.text for row_btns in kb.inline_keyboard for btn in row_btns)
    blob = text + labels
    assert "Свой макет: нет" in text
    assert "Загрузить макет" in labels
    assert "qr_card_" not in blob
    assert re.search(r"#[0-9A-Fa-f]{6}", blob) is None
    for data in ("qrct_layout", "qrct_layout_rm", "qrct_layout_rm_go"):
        assert len(data.encode()) < 64
        assert required_capability(callback_data=data) == "settings"
    for data in _flat_callback_data(kb):
        if data is None:
            continue
        assert len(data.encode()) < 64
        assert required_capability(callback_data=data) == "settings", data

    stranger = Principal(telegram_id=1, via="initdata", caps=frozenset(), city=None)
    try:
        asyncio.run(layout_api.layout_preview(None, stranger))
        raise AssertionError("ждали 403")
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "право настроек" in exc.detail["text"]

    asyncio.run(db.qr_card_layout_put("FILE", {"qr": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4}, "name": {"x": 0.1, "y": 0.7, "w": 0.4, "h": 0.1}}))
    manager = Principal(telegram_id=1, via="initdata", caps=frozenset({"settings"}), city=None)
    try:
        asyncio.run(layout_api.layout_save(layout_api.MarkupIn(markup={"city": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}}), manager))
        raise AssertionError("ждали 400")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "рамку кода" in exc.detail["text"]
    still = asyncio.run(db.qr_card_layout_get())
    assert still["file_id"] == "FILE"
    assert "name" in still["markup"]

    asyncio.run(layout_api.layout_save(layout_api.MarkupIn(markup={
        "qr": {"x": 0.2, "y": 0.2, "w": 0.4, "h": 0.4},
        "logo": {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2},
        "vk": {"x": 0.5, "y": 0.8, "w": 0.3, "h": 0.08},
    }), manager))
    saved = asyncio.run(db.qr_card_layout_get())
    assert saved["file_id"] == "FILE"
    assert "logo" not in saved["markup"]
    assert "vk" in saved["markup"]
    assert "name" not in saved["markup"]

    guide = Path("docs/BOT_GUIDE.md").read_text(encoding="utf-8")
    item = guide.split("6. **Баллы.**", 1)[0].split("5. **Чек-ин.**", 1)[1]
    assert "макет" in item
    assert "прошёл" in item
    cert_py = Path("miniapp/routers/certificate_markup.py").read_text(encoding="utf-8")
    cert_js = Path("miniapp/static/js/screens/certificate_markup.js").read_text(encoding="utf-8")
    assert "qr-card-layout" not in cert_py
    assert "qr-card-layout" not in cert_js
