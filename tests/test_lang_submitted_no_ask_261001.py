"""Приёмка 01.10: одобренный делегат без сохранённого языка на /start не получает «Выберите язык
анкеты» — анкета давно подана (так у импортированных делегатов прошлого сезона). Русский по
умолчанию, в users.lang ничего не пишется; новичку без анкеты экран по-прежнему показывается.
"""
import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers.reg import reg_lang
from tests._dbtpl import fast_init_db

UID = 8261001


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_lang_submitted_no_ask_261001.db")
    fast_init_db()


async def _enable(mode):
    async with db._connect() as conn:
        for key, value in (("delegate_lang_enabled", "on"), ("delegate_lang_ask_on_start", mode)):
            await conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        await conn.commit()


class _User:
    def __init__(self, uid, language_code):
        self.id = uid
        self.language_code = language_code
        self.username = None


class _Msg:
    def __init__(self, uid, language_code):
        self.from_user = _User(uid, language_code)
        self.sent = []

    async def answer(self, text=None, reply_markup=None, **k):
        self.sent.append(text)


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=UID, user_id=UID))


@pytest.mark.parametrize("mode,client", [("everyone", "ru"), ("on", "en")])
@pytest.mark.parametrize("status", ["approved", "pending"])
def test_submitted_delegate_without_lang_not_asked(tmp_path, mode, client, status):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable(mode)
        await db.add_user({"telegram_id": UID, "full_name": "Иванов Иван", "registration_date": "2026-03-01 10:00:00"})
        await db.set_user_status(UID, status)
        msg = _Msg(UID, client)
        shown = await reg_lang.offer_language(msg, _state())
        assert shown is False
        assert msg.sent == []
        assert (await db.get_user(UID)).get("lang") in (None, "")

    asyncio.run(go())


def test_newcomer_without_form_still_asked(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable("everyone")
        msg = _Msg(UID, "ru")
        assert await reg_lang.offer_language(msg, _state()) is True
        assert msg.sent and "язык" in msg.sent[0]

    asyncio.run(go())


def test_rejected_without_lang_still_asked(tmp_path):
    """Отказ — делегат может подавать заново, язык новой анкеты спрашиваем как раньше."""
    _use_tmp_db(tmp_path)

    async def go():
        await _enable("everyone")
        await db.add_user({"telegram_id": UID, "full_name": "Иванов Иван", "registration_date": "2026-03-01 10:00:00"})
        await db.set_user_status(UID, "rejected")
        assert await reg_lang.offer_language(_Msg(UID, "ru"), _state()) is True

    asyncio.run(go())


def test_menu_lang_button_for_submitted_delegate_asks_bot_language(tmp_path):
    """Кнопка «🌐 Язык / Language» у подавшего анкету — про язык бота, не анкеты."""
    _use_tmp_db(tmp_path)

    async def go():
        await _enable("on")
        newcomer = _Msg(UID, "ru")
        await reg_lang.menu_lang_open(newcomer)
        await db.add_user({"telegram_id": UID, "full_name": "Иванов Иван", "registration_date": "2026-03-01 10:00:00"})
        await db.set_user_status(UID, "approved")
        submitted = _Msg(UID, "ru")
        await reg_lang.menu_lang_open(submitted)
        return newcomer.sent, submitted.sent

    newcomer, submitted = asyncio.run(go())
    assert newcomer == ["Выберите язык анкеты / Choose the form language"]
    assert submitted == ["Выберите язык / Choose language"]
