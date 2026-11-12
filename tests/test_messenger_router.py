"""Роутер пишет в один мессенджер. Номер ВК на карточке сам по себе вторую ленту не включает."""
import asyncio
import logging

from services.messenger_router import send_to_delegate


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _patch(monkeypatch, user):
    vk_sent = []

    async def _get_user(_user_id):
        return user

    async def _send_vk(vk_id, text, keyboard_json=None):
        vk_sent.append((vk_id, text, keyboard_json))

    monkeypatch.setattr("services.messenger_router.get_user", _get_user)
    monkeypatch.setattr("services.messenger_router.send_vk_message", _send_vk)
    return vk_sent


def test_route_to_telegram(monkeypatch):
    bot = _Bot()
    vk_sent = _patch(monkeypatch, {
        "telegram_id": 10,
        "vk_id": 77,
        "active_messenger": "telegram",
    })

    asyncio.run(send_to_delegate(10, "привет", bot=bot))

    assert bot.sent == [(10, "привет")]
    assert vk_sent == []


def test_route_to_vk(monkeypatch):
    bot = _Bot()
    vk_sent = _patch(monkeypatch, {
        "telegram_id": 10,
        "vk_id": 77,
        "active_messenger": "vk",
    })

    asyncio.run(send_to_delegate(10, "привет", bot=bot, keyboard_json='{"buttons":[]}'))

    assert vk_sent == [(77, "привет", '{"buttons":[]}')]
    assert bot.sent == []


def test_route_default_telegram(monkeypatch):
    bot = _Bot()
    vk_sent = _patch(monkeypatch, {
        "telegram_id": 10,
        "vk_id": 77,
        "active_messenger": None,
    })

    asyncio.run(send_to_delegate(10, "привет", bot=bot))

    assert bot.sent == [(10, "привет")]
    assert vk_sent == []


def test_route_missing_vk_id(monkeypatch, caplog):
    bot = _Bot()
    vk_sent = _patch(monkeypatch, {
        "telegram_id": 10,
        "vk_id": None,
        "active_messenger": "vk",
    })

    with caplog.at_level(logging.ERROR):
        asyncio.run(send_to_delegate(10, "секретный текст", bot=bot))

    assert vk_sent == []
    assert bot.sent == []
    assert "номера ВК нет" in caplog.text
    assert "секретный текст" not in caplog.text
