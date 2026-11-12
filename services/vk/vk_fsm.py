"""Состояние анкеты ВК в той же fsm_storage, но не в ключе Telegram.

bot_id 0 и destiny vk не встречаются у поллинга aiogram: там настоящий id бота
и destiny default. Перезапуск состояние не стирает.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.storage.base import StorageKey

from database.fsm_storage import SqliteStorage

_VK_BOT_ID = 0
_DESTINY = "vk"


def vk_storage_key(vk_id: int) -> StorageKey:
    vk_id = int(vk_id)
    return StorageKey(bot_id=_VK_BOT_ID, chat_id=vk_id, user_id=vk_id, destiny=_DESTINY)


async def _run(callback):
    storage = SqliteStorage()
    try:
        return await callback(storage)
    finally:
        await storage.close()


async def get_vk_fsm_state(vk_id: int) -> str | None:
    key = vk_storage_key(vk_id)

    async def _read(storage: SqliteStorage):
        return await storage.get_state(key)

    return await _run(_read)


async def set_vk_fsm_state(vk_id: int, state: str) -> None:
    key = vk_storage_key(vk_id)

    async def _write(storage: SqliteStorage):
        await storage.set_state(key, state)

    await _run(_write)


async def clear_vk_fsm_state(vk_id: int) -> None:
    key = vk_storage_key(vk_id)

    async def _clear(storage: SqliteStorage):
        await storage.set_data(key, {})
        await storage.set_state(key, None)

    await _run(_clear)


async def get_vk_fsm_data(vk_id: int) -> dict[str, Any]:
    key = vk_storage_key(vk_id)

    async def _read(storage: SqliteStorage):
        return await storage.get_data(key)

    return await _run(_read)


async def set_vk_fsm_data(vk_id: int, data: Mapping[str, Any]) -> None:
    key = vk_storage_key(vk_id)
    payload = dict(data)

    async def _write(storage: SqliteStorage):
        await storage.set_data(key, payload)

    await _run(_write)
