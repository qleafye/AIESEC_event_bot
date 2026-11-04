"""Оформленная карточка QR делегата (`services.forum.qr_card`) и откат на голый QR.

Содержимое кода не проверяем декодером (его нет в окружении) — формат строки покрыт
`tests/test_checkin_qr_260923.py`; здесь только то, что карточка собирается и что сбой рендера
не оставляет делегата без пропуска."""
from __future__ import annotations

import asyncio
import io

from PIL import Image

from services.forum import checkin as checkin_mod
from services.forum.qr_card import pass_vk_line, render_qr_card
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


def test_pass_vk_line_turns_nick_into_address():
    assert pass_vk_line("@ivan_lead") == "vk.com/ivan_lead"
    assert pass_vk_line("  ivan.lead ") == "vk.com/ivan.lead"
    assert pass_vk_line("https://vk.com/ivan") == "vk.com/ivan"
    assert pass_vk_line("https://m.vk.com/ivan") == "vk.com/ivan"
    assert pass_vk_line("https://instagram.com/ivan") == "instagram.com/ivan"
    assert pass_vk_line("Иван Петров") == "ВК: Иван Петров"
    assert pass_vk_line("") == ""
    assert pass_vk_line(None) == ""
    assert pass_vk_line("-") == ""
    assert pass_vk_line("нет") == ""
    assert pass_vk_line("@") == ""


def _hint_sits_below_card(png: bytes) -> bool:
    """Оранжевая плашка ниже белой карточки, между ними синий фон. Белые буквы
    заголовка и подписи на самой плашке не считаются карточкой."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    x = img.size[0] // 2
    orange_y = None
    for y in range(img.size[1]):
        r, g, b = img.getpixel((x, y))
        if r > 180 and 70 < g < 190 and b < 90:
            orange_y = y
            break
    if orange_y is None or orange_y < 16:
        return False
    r, g, b = img.getpixel((x, orange_y - 8))
    if not (b > 140 and r < 90):
        return False
    for y in range(orange_y - 8):
        r, g, b = img.getpixel((x, y))
        if r > 230 and g > 230 and b > 230:
            return True
    return False


def test_vk_line_sits_on_the_card_not_on_the_hint():
    plain = render_qr_card(
        "YL26·Иванова Анна·Москва·" + "x" * 22, "Иванова Анна", "Москва",
        "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
    )
    with_vk = render_qr_card(
        "YL26·Иванова Анна·Москва·" + "x" * 22, "Иванова Анна", "Москва",
        "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
        "vk.com/ivanova_anna",
    )
    long_name = render_qr_card(
        "YL26·X·Москва·tok", "Константинопольская Александра Сергеевна Длиннофамильная",
        "Москва", "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
        "vk.com/" + "a" * 40,
    )
    assert with_vk != plain
    for png in (with_vk, long_name):
        img = Image.open(io.BytesIO(png))
        assert img.size == (1080, 1440)
        assert img.mode == "P"
        assert len(png) < 90_000
        assert _hint_sits_below_card(png)


def test_build_checkin_qr_draws_vk_from_the_form(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    seen: dict = {}
    real = render_qr_card

    def spy(*args, **kwargs):
        seen["social"] = args[6] if len(args) > 6 else kwargs.get("social", "")
        return real(*args, **kwargs)

    monkeypatch.setattr("services.forum.qr_card.render_qr_card", spy)
    png, _ = asyncio.run(checkin_mod.build_checkin_qr({
        "telegram_id": UID, "full_name": "Иванов Иван", "event_city": "msk",
        "vk_username": "@ivan_lead",
    }))
    assert seen["social"] == "vk.com/ivan_lead"
    assert Image.open(io.BytesIO(png)).size == (1080, 1440)


def test_card_png_is_small_palette():
    """Рассылка QR уходит фото каждому одобренному — карточка должна быть лёгкой (палитра)."""
    png = render_qr_card(
        "YL26·Иванова Анна·Москва·" + "x" * 22, "Иванова Анна", "Москва",
        "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
    )
    img = Image.open(io.BytesIO(png))
    assert img.mode == "P"
    assert len(png) < 90_000
