"""FSM в forum.db переживает закрытие соединения. Тесты на MemoryStorage не трогаем."""
from __future__ import annotations

import asyncio

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey

from config import config
from database import db as bot_db
from database.fsm_storage import SqliteStorage


def _run(coro):
    return asyncio.run(coro)


def test_sqlite_fsm_survives_reopen_and_keeps_thread(tmp_path):
    path = str(tmp_path / "forum.db")
    config.DB_PATH = path
    _run(bot_db.init_db())

    key = StorageKey(bot_id=1, chat_id=10, user_id=20, thread_id=3, destiny="default")
    other_thread = StorageKey(
        bot_id=1, chat_id=10, user_id=20, thread_id=7, destiny="default",
    )
    no_thread = StorageKey(bot_id=1, chat_id=10, user_id=20, thread_id=None, destiny="default")
    other_destiny = StorageKey(
        bot_id=1, chat_id=10, user_id=20, thread_id=3, destiny="other",
    )
    pair = StorageKey(bot_id=2, chat_id=1, user_id=1)

    async def scenario():
        first = SqliteStorage(path)
        await first.set_state(key, State("name", "Registration"))
        await first.set_data(key, {"name": "Аня", "n": 1})
        merged = await first.update_data(key, {"city": "msk"})
        assert merged == {"name": "Аня", "n": 1, "city": "msk"}
        await first.set_data(no_thread, {"plain": True})
        await first.close()

        second = SqliteStorage(path)
        assert await second.get_state(key) == "Registration:name"
        assert await second.get_data(key) == {"name": "Аня", "n": 1, "city": "msk"}
        assert await second.get_state(other_thread) is None
        assert await second.get_data(other_thread) == {}
        assert await second.get_data(no_thread) == {"plain": True}
        assert await second.get_state(other_destiny) is None
        assert await second.get_data(other_destiny) == {}

        await asyncio.gather(
            second.update_data(pair, {"left": 1}),
            second.update_data(pair, {"right": 2}),
        )
        assert await second.get_data(pair) == {"left": 1, "right": 2}

        await second.set_state(key, None)
        await second.set_data(key, {})
        assert await second.get_state(key) is None
        assert await second.get_data(key) == {}
        # соседние ключи сброс одной строки не задевает
        assert await second.get_data(no_thread) == {"plain": True}
        assert await second.get_data(pair) == {"left": 1, "right": 2}
        await second.close()

        async with aiosqlite.connect(path) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM fsm_storage WHERE thread_id IS ?", (3,),
            ) as cursor:
                gone = await cursor.fetchone()
            async with conn.execute("PRAGMA journal_mode") as cursor:
                mode = await cursor.fetchone()
        assert gone[0] == 0
        assert str(mode[0]).lower() == "wal"

    _run(scenario())
