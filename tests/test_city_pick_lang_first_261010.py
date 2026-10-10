"""Приёмка 09.10 (C12): экран языка и кнопки городов оказались в чате одновременно, и тап по
городу продолжил анкету мимо выбора языка — язык так и не был выбран.

Задумано (D-06, `handlers/reg/reg_lang.py`): язык не угадывается молча и спрашивается раньше
города. Тап по городу, пока язык ещё нужно спросить, теперь не продолжает анкету: бот
объясняет, что сначала язык, и показывает экран выбора; после выбора `/start` продолжается
обычным путём (снова город).
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers.reg import reg_flow
from tests._dbtpl import fast_init_db

UID = 862001


class _User:
    def __init__(self, uid, language_code=None):
        self.id = uid
        self.username = "u"
        self.language_code = language_code


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _Msg:
    def __init__(self, uid, sent):
        self.from_user = _User(uid)
        self.chat = _Chat(uid)
        self.sent = sent

    async def answer(self, text=None, *a, **k):
        self.sent.append((text, k.get("reply_markup")))

    async def answer_photo(self, *a, **k):
        self.sent.append(("<photo>", k.get("reply_markup")))

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _Msg(self.chat.id, self.sent)
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _Callback:
    def __init__(self, data, uid, language_code="ru"):
        self.data = data
        self.from_user = _User(uid, language_code)
        self.sent = []
        self.message = _Msg(uid, self.sent)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _datas(markup):
    rows = getattr(markup, "inline_keyboard", None) or []
    return [b.callback_data for row in rows for b in row]


async def _setup(tmp_path, ask_on_start="everyone"):
    config.DB_PATH = str(tmp_path / "c12.db")
    fast_init_db()
    await db.set_setting("event_city_enabled", "on")
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('delegate_lang_enabled', 'on')")
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('delegate_lang_ask_on_start', ?)",
            (ask_on_start,))
        await conn.commit()


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=UID, user_id=UID))


def test_city_tap_before_language_choice_shows_language_screen(tmp_path, monkeypatch):
    continued = []

    async def _spy(*a, **k):
        continued.append(a)

    monkeypatch.setattr(reg_flow, "_continue_after_city", _spy)

    async def go():
        await _setup(tmp_path)
        cb = _Callback("city_pick:spb", UID)
        await reg_flow.city_pick(cb, _state())
        return cb

    cb = asyncio.run(go())
    assert continued == []
    picker = [m for (_, m) in cb.sent if any(d.startswith("lang_pick:") for d in _datas(m))]
    assert len(picker) == 1
    assert _datas(picker[0]) == ["lang_pick:ru:start", "lang_pick:en:start"]
    assert not any(t and t.startswith("✅") and "Город" in t for (t, _) in cb.sent)
    assert any(text and "язык" in text.lower() for (text, _) in cb.answers)


def test_city_tap_after_language_choice_continues(tmp_path, monkeypatch):
    continued = []

    async def _spy(*a, **k):
        continued.append(a)

    monkeypatch.setattr(reg_flow, "_continue_after_city", _spy)

    async def go():
        await _setup(tmp_path)
        await db.add_user({"telegram_id": UID, "full_name": "Т Т", "registration_date": "2026-10-01 10:00:00"})
        await db.set_user_lang(UID, "ru")
        cb = _Callback("city_pick:spb", UID)
        await reg_flow.city_pick(cb, _state())
        return cb

    cb = asyncio.run(go())
    assert len(continued) == 1
    assert not any(d.startswith("lang_pick:") for (_, m) in cb.sent for d in _datas(m))


def test_city_tap_with_language_module_off_continues(tmp_path, monkeypatch):
    continued = []

    async def _spy(*a, **k):
        continued.append(a)

    monkeypatch.setattr(reg_flow, "_continue_after_city", _spy)

    async def go():
        await _setup(tmp_path, ask_on_start="off")
        cb = _Callback("city_pick:spb", UID)
        await reg_flow.city_pick(cb, _state())
        return cb

    cb = asyncio.run(go())
    assert len(continued) == 1
