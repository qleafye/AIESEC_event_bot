"""Фейки чата для тестов делегатского потока записи на сессии (сообщение, колбэк, FSM)."""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from cities import per_city_key
from database import db
from tests._enroll38 import CITY, seed_delegates, seed_msk_program


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = "Тест"


class FakeChat:
    def __init__(self, cid):
        self.id = cid
        self.type = "private"


class FakeMessage:
    def __init__(self, user_id, text=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.answers: list[tuple[str, object]] = []
        self.edits: list[tuple[str, object]] = []
        self.photos: list[tuple[object, object]] = []
        self.fail_photo = False

    async def answer(self, text=None, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))
        return self

    async def answer_photo(self, photo, caption=None, **kw):
        if self.fail_photo:
            raise RuntimeError("photo boom")
        self.photos.append((photo, caption))
        return self

    @property
    def last(self):
        """Последний показанный экран (правка или новое сообщение)."""
        return (self.edits or self.answers)[-1]


class FakeCallback:
    def __init__(self, data, user_id, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeMessage(user_id)
        self.alerts: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append((text, show_alert))


def make_state(uid=1):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def buttons(markup):
    """[(text, callback_data)] всех кнопок клавиатуры."""
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def texts_of(markup):
    return [t for t, _ in buttons(markup)]


async def setup_world(enabled=True):
    ids = await seed_msk_program()
    await seed_delegates()
    await db.set_setting("event_city_enabled", "on")
    if enabled:
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
    return ids
