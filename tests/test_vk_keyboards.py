"""Клавиатура ВК: нет тупика. Пункт без ответа бота не рисуется."""
import asyncio
import json

import keyboards.vk_builders as vk_kb
from keyboards.builders import MENU_BUTTONS
from keyboards.vk_builders import vk_main_menu_keyboard


def _patch(monkeypatch, handled, values, captions):
    monkeypatch.setattr(vk_kb, "VK_HANDLED_MENU_KEYS", frozenset(handled))

    async def _setting(key, city):
        assert city == "msk"
        return values.get(key, "off")

    async def _caption(key, city, lang="ru", **_kwargs):
        assert city == "msk"
        assert lang == "ru"
        return captions[key]

    monkeypatch.setattr(vk_kb, "get_setting_typed_for_city", _setting)
    monkeypatch.setattr(vk_kb, "caption_for", _caption)


def _labels(raw: str) -> list[list[str]]:
    data = json.loads(raw)
    return [[button["action"]["label"] for button in row] for row in data["buttons"]]


def test_empty_until_vk_answers(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise AssertionError("настройки не читаются, пока отвечать не на что")

    monkeypatch.setattr(vk_kb, "get_setting_typed_for_city", _boom)
    raw = asyncio.run(vk_main_menu_keyboard(1))
    data = json.loads(raw)
    assert data == {"one_time": False, "buttons": []}


def test_miniapp_filtered(monkeypatch):
    _patch(
        monkeypatch,
        {"menu_miniapp", "menu_info"},
        {"menu_miniapp": "on", "menu_info": "on"},
        {"menu_info": "О форуме"},
    )
    raw = asyncio.run(vk_main_menu_keyboard(5, "msk"))
    data = json.loads(raw)
    assert data["one_time"] is False
    assert _labels(raw) == [["О форуме"]]
    assert "Приложение" not in raw
    assert data["buttons"][0][0]["action"]["type"] == "text"


def test_unhandled_button_hidden_even_if_on(monkeypatch):
    _patch(
        monkeypatch,
        {"menu_referral"},
        {"menu_referral": "on", "menu_contacts": "on"},
        {"menu_referral": "Ссылка"},
    )
    assert _labels(asyncio.run(vk_main_menu_keyboard(5, "msk"))) == [["Ссылка"]]


def test_keyboard_json_structure(monkeypatch):
    _patch(
        monkeypatch,
        {"menu_info"},
        {"menu_info": "on"},
        {"menu_info": "О форуме"},
    )
    data = json.loads(asyncio.run(vk_main_menu_keyboard(5, "msk")))
    assert set(data) == {"one_time", "buttons"}
    action = data["buttons"][0][0]["action"]
    assert action == {"type": "text", "label": "О форуме"}


def test_two_buttons_per_row(monkeypatch):
    handled = ["menu_referral", "menu_invites", "menu_info"]
    _patch(
        monkeypatch,
        handled,
        {key: "on" for key in handled},
        {
            "menu_referral": "Ссылка",
            "menu_invites": "Приглашённые",
            "menu_info": "О форуме",
        },
    )
    rows = _labels(asyncio.run(vk_main_menu_keyboard(5, "msk")))
    assert rows == [["Ссылка", "Приглашённые"], ["О форуме"]]
    order = [key for key, _text in MENU_BUTTONS if key in handled]
    assert order == handled


def test_off_button_skipped(monkeypatch):
    _patch(
        monkeypatch,
        {"menu_referral", "menu_info"},
        {"menu_referral": "off", "menu_info": "on"},
        {"menu_info": "О форуме"},
    )
    assert _labels(asyncio.run(vk_main_menu_keyboard(5, "msk"))) == [["О форуме"]]
