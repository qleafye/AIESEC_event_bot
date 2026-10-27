"""Оформленная карточка QR делегата (`services.forum.qr_card`) и откат на голый QR.

Содержимое кода не проверяем декодером (его нет в окружении) — формат строки покрыт
`tests/test_checkin_qr_260923.py`; здесь только то, что карточка собирается и что сбой рендера
не оставляет делегата без пропуска."""
from __future__ import annotations

import asyncio
import io

from PIL import Image

from services.forum import checkin as checkin_mod
from services.forum.qr_card import render_qr_card
from tests.test_checkin_qr_260923 import UID, _seed_user, _use_tmp_db


def test_render_qr_card_size_and_long_name():
    png = render_qr_card(
        "YL26·X·Москва·tok", "Константинопольская Александра Сергеевна Длиннофамильная",
        "Москва", "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
    )
    assert Image.open(io.BytesIO(png)).size == (1080, 1440)


def test_build_checkin_qr_sends_card(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    png, caption = asyncio.run(checkin_mod.build_checkin_qr({
        "telegram_id": UID, "full_name": "Иванов Иван", "event_city": "msk",
    }))
    assert Image.open(io.BytesIO(png)).size == (1080, 1440)
    assert caption


def test_build_checkin_qr_falls_back_to_plain_qr(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _seed_user(UID)

    async def broken(user, payload):
        raise OSError("нет шрифта")

    monkeypatch.setattr(checkin_mod, "_render_card", broken)
    png, _ = asyncio.run(checkin_mod.build_checkin_qr({
        "telegram_id": UID, "full_name": "Иванов Иван", "event_city": "msk",
    }))
    width, height = Image.open(io.BytesIO(png)).size
    assert width == height and width != 1080
