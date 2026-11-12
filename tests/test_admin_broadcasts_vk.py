"""Рассылка ВК — отдельная кнопка. В список попадает лента ВК, не всякий номер и не ник."""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers.comms import admin_broadcasts
from handlers.comms.admin_broadcast_vk import vk_broadcast_go, vk_broadcast_start, vk_broadcast_text
from handlers.states import Broadcast
from services.vk_broadcast import list_vk_broadcast_ids


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_broadcast_admin.db")


def _state(user_id=1):
    storage = MemoryStorage()
    return FSMContext(
        storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


class _User:
    def __init__(self, uid):
        self.id = uid


class _Message:
    def __init__(self, text="", photo=None):
        self.text = text
        self.photo = photo
        self.video = None
        self.document = None
        self.animation = None
        self.sticker = None
        self.audio = None
        self.voice = None
        self.from_user = _User(1)
        self.answers = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))
        self.text = text


class _Callback:
    def __init__(self, message):
        self.message = message
        self.from_user = _User(1)
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append((text, show_alert))


def test_vk_audience_filter(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _empty():
        state = _state()
        await state.set_state(Broadcast.target_selection)
        message = _Message()
        await vk_broadcast_start(_Callback(message), state)
        return message.text, await state.get_state()

    empty_text, empty_state = asyncio.run(_empty())
    assert "никому не пишем" in empty_text
    assert empty_state == Broadcast.target_selection.state

    async def _go():
        await db.add_user({
            "telegram_id": 100,
            "registration_date": "2026-11-12 12:00:00",
            "vk_username": "@nick",
        })
        await db.add_user({
            "telegram_id": 101,
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.link_vk_account(101, 11)
        await db.add_user({
            "telegram_id": 102,
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.link_vk_account(102, 12)
        await db.set_active_messenger(102, "vk")
        await db.add_user({
            "telegram_id": 103,
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.set_active_messenger(103, "vk")
        await db.create_vk_user(55, {"full_name": "Только ВК"})
        return await list_vk_broadcast_ids()

    assert asyncio.run(_go()) == [12, 55]


def test_vk_confirm_count(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.create_vk_user(55, {"full_name": "Один"})
        await db.create_vk_user(56, {"full_name": "Два"})
        await db.add_user({
            "telegram_id": 101,
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.link_vk_account(101, 11)
        state = _state()
        await state.set_state(Broadcast.vk_text)
        photo = _Message(text="с картинкой", photo=["file"])
        await vk_broadcast_text(photo, state)
        message = _Message(text="Добро пожаловать")
        await vk_broadcast_text(message, state)
        menu = [b.callback_data for row in admin_broadcasts.build_broadcast_menu_kb(1).inline_keyboard for b in row]
        return photo.answers, message.answers, await state.get_state(), menu

    photo_answers, answers, state_name, menu = asyncio.run(_go())
    assert "только текст" in photo_answers[0][0]
    assert state_name == Broadcast.vk_confirm.state
    assert "получателей 2" in answers[0][0]
    assert "broadcast_vk" in menu
    buttons = [b.callback_data for row in answers[0][1].inline_keyboard for b in row]
    assert buttons == ["bcvk_go", "bcvk_no"]


def test_vk_send_calls_broadcast_vk(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    calls = []

    async def _broadcast(text, photo_file_id, vk_ids):
        calls.append((text, photo_file_id, list(vk_ids)))
        return (1, 0)

    monkeypatch.setattr("handlers.comms.admin_broadcast_vk.broadcast_vk", _broadcast)

    async def _go():
        await db.create_vk_user(55, {"full_name": "Один"})
        await db.add_user({
            "telegram_id": 101,
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.link_vk_account(101, 11)
        state = _state()
        await state.set_state(Broadcast.vk_confirm)
        await state.update_data(vk_text="Добро пожаловать")
        message = _Message()
        await vk_broadcast_go(_Callback(message), state)
        return message.text, await state.get_state()

    text, state_name = asyncio.run(_go())
    assert calls == [("Добро пожаловать", None, [55])]
    assert text == "Рассылка ВК: 1 доставлено, 0 ошибок."
    assert state_name is None
