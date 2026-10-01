"""Программа у делегата: чат и Mini App по одному правилу (тумблер «Таблица/Фото» города).

Конвенция соседей (`tests/test_program_view_260924.py`): Fake-объекты, БД — tmp_path
(`fast_init_db`), `asyncio.run()`."""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from cities import per_city_key
from config import config
from database import db
from services import program
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 961003001
MANAGER_ID = 961003002
SPB_DELEGATE = 961003010
TMN_DELEGATE = 961003011


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, cities_on=True):
    config.DB_PATH = str(tmp_path / "program_photo_city.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    if cities_on:
        _run(db.set_setting("event_city_enabled", "on"))


def _delegate(tid, city):
    _run(db.add_user({
        "telegram_id": tid, "full_name": "Делегат", "registration_date": "2026-09-01",
        "event_city": city, "status": "approved",
    }))


class _User:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = "Тест"


class _Chat:
    def __init__(self, cid):
        self.id = cid
        self.type = "private"


class _Msg:
    def __init__(self, user_id, text=None, photo=None, caption=None):
        self.from_user = _User(user_id)
        self.chat = _Chat(user_id)
        self.text = text
        self.caption = caption
        self.html_text = caption or text
        self.photo = photo
        self.photos_sent: list = []
        self.texts: list = []
        self.edited = None
        self.bot = None

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        self.texts.append(text)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.photos_sent.append(photo)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text


class _Photo:
    def __init__(self, file_id):
        self.file_id = file_id


class _Callback:
    def __init__(self, data, user_id=SUPERADMIN_ID):
        self.data = data
        self.from_user = _User(user_id)
        self.message = _Msg(user_id)
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _show(tid):
    from handlers import user_actions

    msg = _Msg(tid, text="📅 Программа форума")
    _run(user_actions.show_program(msg))
    return msg


# ── Показ: чат по тумблеру, как Mini App ────────────────────────────────────────────────────

def test_chat_follows_table_toggle_even_when_city_has_own_photo(tmp_path):
    _ready(tmp_path)
    _delegate(SPB_DELEGATE, "spb")
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB_PHOTO"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting(per_city_key("program_miniapp_view", "spb"), "table"))
    msg = _show(SPB_DELEGATE)
    assert msg.photos_sent == []  # раньше любое фото перекрывало сессии в чате
    assert any("Открытие" in t for t in msg.texts)
    assert _run(program.resolve_program_content("spb"))[0] == "table"  # Mini App — то же


def test_chat_shows_own_photo_when_toggle_is_photo(tmp_path):
    _ready(tmp_path)
    _delegate(SPB_DELEGATE, "spb")
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB_PHOTO"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting(per_city_key("program_miniapp_view", "spb"), "photo"))
    assert _show(SPB_DELEGATE).photos_sent == ["SPB_PHOTO"]
