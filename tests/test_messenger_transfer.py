"""«Пиши сюда» переносит ленту на эту карточку. Без склейки вторая лента не включается."""
import asyncio

from config import config
from database import db
from handlers.user_actions import transfer_feed_here
from handlers.vk.vk_bot import handle_vk_message
from services.messenger_router import send_to_delegate


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "messenger_transfer.db")


class _Message:
    def __init__(self, user_id):
        self.from_user = type("U", (), {"id": user_id})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _capture_vk(monkeypatch):
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text, keyboard_json))

    monkeypatch.setattr("handlers.vk.vk_bot.send_vk_message", _send)
    monkeypatch.setattr("services.messenger_router.send_vk_message", _send)
    return sent


def test_transfer_to_telegram(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    vk_sent = _capture_vk(monkeypatch)

    async def _go():
        missing = _Message(404)
        await transfer_feed_here(missing)
        await db.add_user({
            "telegram_id": 100,
            "registration_date": "2026-11-12 12:00:00",
            "full_name": "Иван Иванов",
        })
        await db.link_vk_account(100, 7)
        await db.set_active_messenger(100, "vk")
        message = _Message(100)
        await transfer_feed_here(message)
        bot = _Bot()
        await send_to_delegate(100, "письмо", bot=bot)
        return missing.answers, message.answers, await db.get_user(100), bot.sent

    missing, answers, row, telegram_sent = asyncio.run(_go())
    assert "анкету" in missing[0]
    assert row["active_messenger"] == "telegram"
    assert row["vk_id"] == 7
    assert any("сюда" in text for text in answers)
    assert telegram_sent == [(100, "письмо")]
    assert vk_sent == []


def test_transfer_to_vk(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    vk_sent = _capture_vk(monkeypatch)

    async def _go():
        await db.add_user({
            "telegram_id": 100,
            "registration_date": "2026-11-12 12:00:00",
            "full_name": "Иван Иванов",
            "vk_username": "@nick",
        })
        await db.link_vk_account(100, 7)
        before = await db.get_user(100)
        await handle_vk_message(7, "Пиши сюда")
        after = await db.get_user(100)
        bot = _Bot()
        await send_to_delegate(100, "письмо", bot=bot)
        return before, after, bot.sent

    before, after, telegram_sent = asyncio.run(_go())
    assert after["active_messenger"] == "vk"
    before["active_messenger"] = "vk"
    assert after == before
    assert telegram_sent == []
    assert [item[1] for item in vk_sent] == [
        "Теперь письма будут приходить сюда.",
        "письмо",
    ]


def test_transfer_unlinked(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    vk_sent = _capture_vk(monkeypatch)
    started = []

    async def _start(vk_id):
        started.append(vk_id)

    async def _step(vk_id, text):
        started.append((vk_id, text))

    monkeypatch.setattr("handlers.vk.vk_registration.start_vk_registration", _start)
    monkeypatch.setattr("handlers.vk.vk_registration.process_vk_registration_step", _step)

    async def _go():
        await db.create_vk_user(7, {"full_name": "Иван Иванов"})
        before = await db.get_vk_user(7)
        await handle_vk_message(7, "Пиши сюда")
        await handle_vk_message(7, "Пиши сюда, пожалуйста")
        after = await db.get_vk_user(7)
        return before, after

    before, after = asyncio.run(_go())
    assert after == before
    assert after["telegram_id"] is None
    assert after["active_messenger"] == "vk"
    assert vk_sent[0][1].startswith("Аккаунты ещё не связаны")
    assert started == [(7, "Пиши сюда, пожалуйста")]
