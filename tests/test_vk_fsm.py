"""Состояние анкеты ВК не задевает состояние Telegram в той же базе."""
import asyncio

from aiogram.fsm.storage.base import StorageKey

from config import config
from database import db
from database.fsm_storage import SqliteStorage
from services.vk.vk_fsm import (
    clear_vk_fsm_state,
    get_vk_fsm_data,
    get_vk_fsm_state,
    set_vk_fsm_data,
    set_vk_fsm_state,
    vk_storage_key,
)


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_fsm.db")


def test_set_get_and_clear_state(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await set_vk_fsm_state(77, "full_name")
        await set_vk_fsm_data(77, {"answers": {"event_city": "msk"}})
        await set_vk_fsm_state(77, "age")
        kept = await get_vk_fsm_data(77)
        state = await get_vk_fsm_state(77)
        await clear_vk_fsm_state(77)
        return state, kept, await get_vk_fsm_state(77), await get_vk_fsm_data(77)

    state, kept, cleared, empty = asyncio.run(_go())
    assert state == "age"
    assert kept["answers"]["event_city"] == "msk"
    assert cleared is None
    assert empty == {}


def test_vk_fsm_isolation(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await set_vk_fsm_state(123, "age")
        telegram = StorageKey(bot_id=4242, chat_id=123, user_id=123)
        storage = SqliteStorage()
        try:
            await storage.set_state(telegram, "Registration:full_name")
            telegram_state = await storage.get_state(telegram)
        finally:
            await storage.close()
        vk_state = await get_vk_fsm_state(123)
        key = vk_storage_key(123)
        return vk_state, telegram_state, key.bot_id, key.destiny

    vk_state, telegram_state, bot_id, destiny = asyncio.run(_go())
    assert vk_state == "age"
    assert telegram_state == "Registration:full_name"
    assert bot_id == 0
    assert destiny == "vk"
    assert telegram_state != vk_state
