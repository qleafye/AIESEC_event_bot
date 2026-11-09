"""FSM бота в том же forum.db: анкета и админские сценарии переживают перезапуск.

Строка — все поля StorageKey, включая thread_id. state — строка или NULL.
data — JSON. set_state(None) при пустом data удаляет строку (так сбрасывает
miniapp_outbox). Объекты в data не пишем: json.dumps без pickle.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import aiosqlite
from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from config import config

# thread_id у Telegram — id сообщения темы, он положительный. -1 в индексе
# только вместо NULL, иначе UNIQUE в SQLite не считает два NULL одним ключом.
_WHERE = (
    "bot_id = ? AND chat_id = ? AND user_id = ? "
    "AND thread_id IS ? AND business_connection_id IS ? AND destiny = ?"
)


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS fsm_storage ("
        "bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "thread_id INTEGER, business_connection_id TEXT, destiny TEXT NOT NULL, "
        "state TEXT, data TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fsm_storage_key ON fsm_storage ("
        "bot_id, chat_id, user_id, IFNULL(thread_id, -1), "
        "IFNULL(business_connection_id, ''), destiny)"
    )


def _params(key: StorageKey) -> tuple:
    return (
        key.bot_id, key.chat_id, key.user_id, key.thread_id,
        key.business_connection_id, key.destiny,
    )


def _state_text(state: StateType) -> str | None:
    if isinstance(state, State):
        return state.state
    return state


def _require_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        msg = f"Data must be a dict or dict-like object, got {type(data).__name__}"
        raise DataNotDictLikeError(msg)
    return data


class SqliteStorage(BaseStorage):
    """Одно соединение и один Lock: aiosqlite нельзя звать из двух корутин сразу."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path if path is not None else config.DB_PATH
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None

    async def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            from database.db import DB_BUSY_TIMEOUT_MS

            self._conn = await aiosqlite.connect(
                self._path,
                timeout=DB_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            # journal_mode не трогаем: файл уже в WAL после init_db, PRAGMA его не выключает.
            await self._conn.execute(f"PRAGMA busy_timeout = {int(DB_BUSY_TIMEOUT_MS)}")
        return self._conn

    async def _fetch(self, conn: aiosqlite.Connection, key: StorageKey):
        cursor = await conn.execute(
            f"SELECT state, data FROM fsm_storage WHERE {_WHERE}", _params(key),
        )
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def _write(
        self, conn: aiosqlite.Connection, key: StorageKey,
        state: str | None, data: dict[str, Any],
    ) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        row = await self._fetch(conn, key)
        if row is None:
            await conn.execute(
                "INSERT INTO fsm_storage ("
                "bot_id, chat_id, user_id, thread_id, business_connection_id, destiny, "
                "state, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*_params(key), state, payload),
            )
            return
        await conn.execute(
            f"UPDATE fsm_storage SET state = ?, data = ? WHERE {_WHERE}",
            (state, payload, *_params(key)),
        )

    async def _delete(self, conn: aiosqlite.Connection, key: StorageKey) -> None:
        await conn.execute(f"DELETE FROM fsm_storage WHERE {_WHERE}", _params(key))

    async def _tx(self):
        conn = await self._connection()
        await conn.execute("BEGIN IMMEDIATE")
        return conn

    async def _commit(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("COMMIT")

    async def _rollback(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("ROLLBACK")

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_text = _state_text(state)
        async with self._lock:
            conn = await self._tx()
            try:
                row = await self._fetch(conn, key)
                current = json.loads(row[1]) if row else {}
                if state_text is None and not current:
                    if row is not None:
                        await self._delete(conn, key)
                else:
                    await self._write(conn, key, state_text, current)
                await self._commit(conn)
            except Exception:
                await self._rollback(conn)
                raise

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._lock:
            row = await self._fetch(await self._connection(), key)
        return None if row is None else row[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = _require_dict(data)
        async with self._lock:
            conn = await self._tx()
            try:
                row = await self._fetch(conn, key)
                state_text = None if row is None else row[0]
                if state_text is None and not payload:
                    if row is not None:
                        await self._delete(conn, key)
                else:
                    await self._write(conn, key, state_text, payload)
                await self._commit(conn)
            except Exception:
                await self._rollback(conn)
                raise

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._lock:
            row = await self._fetch(await self._connection(), key)
        if row is None or not row[1]:
            return {}
        loaded = json.loads(row[1])
        return loaded if isinstance(loaded, dict) else {}

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """Чтение и запись одной строки — одна транзакция, не два отдельных запроса."""
        patch = dict(data)
        async with self._lock:
            conn = await self._tx()
            try:
                row = await self._fetch(conn, key)
                current = json.loads(row[1]) if row else {}
                if not isinstance(current, dict):
                    current = {}
                current.update(patch)
                state_text = None if row is None else row[0]
                if state_text is None and not current:
                    if row is not None:
                        await self._delete(conn, key)
                else:
                    await self._write(conn, key, state_text, current)
                await self._commit(conn)
            except Exception:
                await self._rollback(conn)
                raise
        return current.copy()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
