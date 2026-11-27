"""Сквозные швы ВК: анкета, меню без приложения, склейка, аудитория рассылки.

Лишние вопросы анкеты выключены: менеджер сам решает набор, пустой набор — сданная анкета.
"""
import asyncio
import json

from config import config
from database import db
from handlers.vk.vk_bot import handle_vk_message
from keyboards.vk_builders import vk_main_menu_keyboard
from services.vk_broadcast import broadcast_vk, list_vk_broadcast_ids


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_integration.db")


def _ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())


def _empty_form(monkeypatch, sent):
    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text, keyboard_json))

    async def _city_gate(_event_city):
        return "go", None

    async def _pre(_answers, _meta):
        return []

    async def _enabled(_answers, _city):
        return []

    monkeypatch.setattr("handlers.vk.vk_registration.send_vk_message", _send)
    monkeypatch.setattr("handlers.vk.vk_registration.city_gate", _city_gate)
    monkeypatch.setattr("handlers.vk.vk_registration.pre_flow", _pre)
    monkeypatch.setattr("handlers.vk.vk_registration.enabled_steps", _enabled)


def test_vk_user_completes_registration(monkeypatch, tmp_path):
    _ready(tmp_path)
    sent = []
    _empty_form(monkeypatch, sent)

    asyncio.run(handle_vk_message(123456789, "/start"))
    asyncio.run(handle_vk_message(123456789, "Иван Иванов"))

    user = asyncio.run(db.get_vk_user(123456789))
    assert user is not None
    assert user["vk_id"] == 123456789
    assert user["telegram_id"] is None
    assert user["active_messenger"] == "vk"
    assert user["status"] == "pending"
    assert user["full_name"] == "Иван Иванов"
    assert "Анкета отправлена" in sent[-1][1]
    assert sent[-1][2] is None


def test_vk_menu_hides_unanswered_and_the_app(monkeypatch, tmp_path):
    """Приложение не рисуется, даже если его ключ положить в набор отвечаемых пунктов."""
    _ready(tmp_path)
    monkeypatch.setattr(
        "keyboards.vk_builders.VK_HANDLED_MENU_KEYS",
        frozenset({"menu_game_tasks", "menu_miniapp"}),
    )

    payload = json.loads(asyncio.run(vk_main_menu_keyboard(7)))
    labels = [
        button["action"]["label"]
        for row in payload["buttons"]
        for button in row
    ]

    assert labels == ["🎯 Задания"]
    assert "📱 Приложение" not in labels


def test_link_secret_writes_vk_number_and_keeps_the_feed(monkeypatch, tmp_path):
    _ready(tmp_path)
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append(text)

    monkeypatch.setattr("handlers.vk.vk_bot.send_vk_message", _send)

    async def _go():
        await db.add_user({
            "telegram_id": 111222333,
            "full_name": "Иван Иванов",
            "registration_date": "2026-11-12 12:00:00",
        })
        secret = await db.create_link_secret(111222333)
        await handle_vk_message(55, "/start", ref=f"link_{secret}")
        linked = await db.get_user(111222333)
        await handle_vk_message(56, "ещё раз", ref=f"link_{secret}")
        return linked

    user = asyncio.run(_go())
    assert user["vk_id"] == 55
    assert user["telegram_id"] == 111222333
    assert user["active_messenger"] == "telegram"
    assert "Аккаунты связаны" in sent[0]
    assert "недействительна" in sent[1]
    assert asyncio.run(db.get_vk_user(56)) is None


def test_vk_broadcast_reaches_only_the_current_vk_feed(monkeypatch, tmp_path):
    _ready(tmp_path)

    async def _seed():
        await db.create_vk_user(111, {"full_name": "Только ВК", "status": "pending"})
        await db.add_user({
            "telegram_id": 222,
            "full_name": "Только Telegram",
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.add_user({
            "telegram_id": 333,
            "full_name": "Склейка",
            "vk_username": "@nick",
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.link_vk_account(333, 444)
        await db.add_user({
            "telegram_id": 555,
            "full_name": "Лента ВК",
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.link_vk_account(555, 666)
        await db.set_active_messenger(555, "vk")
        await db.add_user({
            "telegram_id": 777,
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.set_active_messenger(777, "vk")
        return await list_vk_broadcast_ids()

    ids = asyncio.run(_seed())
    assert ids == [111, 666]

    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text))
        return True

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr("services.vk_broadcast.send_vk_message", _send)
    monkeypatch.setattr("services.vk_broadcast.asyncio.sleep", _no_wait)
    result = asyncio.run(broadcast_vk("Текст рассылки", None, ids))

    assert result == (2, 0)
    assert sent == [(111, "Текст рассылки"), (666, "Текст рассылки")]
