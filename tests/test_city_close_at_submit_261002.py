"""02.10: закрытие регистрации на город проверяется и при отправке анкеты в чате бота.

До фикса город проверялся только на старте анкеты: кто начал до закрытия, досдавал после.
Фейки и прогон финализации — из tests/test_city_flow_phase71.py.
"""
import asyncio

import cities
from database import db
from tests._dbtpl import fast_init_db
from tests.test_city_flow_phase71 import (
    _FinalizeFakeMessage,
    _FinalizeFakeState,
    _patch_appenders,
    _run_finalize_and_drain,
    _use_tmp_db,
)


class _Msg(_FinalizeFakeMessage):
    def __init__(self, telegram_id):
        super().__init__(telegram_id)
        self.texts = []

    async def answer(self, text=None, *a, **k):
        self.texts.append(text)


async def _close(code: str):
    await db.set_setting(cities.per_city_key("city_reg_close_date", code), "01.10.2026")


def test_new_submission_to_closed_city_is_not_saved(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    named_calls, main_calls = _patch_appenders(monkeypatch)
    uid = 830001

    async def go():
        fast_init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("full_approval", "manual")
        await _close("spb")
        message = _Msg(uid)
        state = _FinalizeFakeState({"full_name": "Late SPb", "event_city": "spb"})
        await _run_finalize_and_drain(message, state, bot=None)
        return message, await db.get_user(uid)

    message, user = asyncio.run(go())
    assert user is None
    assert named_calls == [] and main_calls == []
    assert any("закрыт" in (t or "").lower() for t in message.texts)


def test_new_submission_to_open_city_still_saved(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _patch_appenders(monkeypatch)
    uid = 830002

    async def go():
        fast_init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("full_approval", "manual")
        await _close("spb")
        message = _FinalizeFakeMessage(uid)
        state = _FinalizeFakeState({"full_name": "Msk", "event_city": "msk"})
        await _run_finalize_and_drain(message, state, bot=None)
        return await db.get_user(uid)

    user = asyncio.run(go())
    assert user is not None and user["event_city"] == "msk"
