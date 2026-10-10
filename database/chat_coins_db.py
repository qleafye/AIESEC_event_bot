"""Монеты из чата: «+N» ответом гейм-менеджера и разовый перенос старых баллов из таблицы.

Обе операции пишут обычные строки `coins` (баланс — по-прежнему SUM(delta)), отличаются
источником: `chat` — «+N» в чате, `transfer` — перенос из Google-таблицы.

«Одно сообщение — одно начисление» держит таблица `chat_coin_awards` с PK (чат, сообщение):
строка награды и строка монет пишутся одной транзакцией, второй «+N» на то же сообщение
упирается в PK и монет не пишет. Перенос — одна строка `coins` на человека: у кого строка
с `source = 'transfer'` уже есть, повторный перенос его пропускает.

Отдельный модуль, а не ещё один блок в `database/db.py` (тот больше 12 тысяч строк).
Соединение — `_db._connect()` через атрибут модуля (тесты подменяют `config.DB_PATH`).
"""
from __future__ import annotations

import aiosqlite

from database import db as _db
from services.infra.timeutil import msk_now

SOURCE_CHAT = "chat"
SOURCE_TRANSFER = "transfer"


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS chat_coin_awards ("
        "chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
        "user_id INTEGER NOT NULL, amount INTEGER NOT NULL, awarded_by INTEGER NOT NULL, "
        "coin_id INTEGER, award_message_id INTEGER, created_at TEXT NOT NULL, "
        "PRIMARY KEY (chat_id, message_id))"
    )


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


async def claim_chat_award(
    *, chat_id: int, message_id: int, user_id: int, amount: int, reason: str,
    awarded_by: int, award_message_id: int | None,
) -> tuple[bool, dict | None]:
    """(True, None) — начислено; (False, прежняя награда) — на это сообщение уже начисляли."""
    stamp = _stamp()
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT OR IGNORE INTO chat_coin_awards "
            "(chat_id, message_id, user_id, amount, awarded_by, award_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, user_id, amount, awarded_by, award_message_id, stamp),
        )
        if cursor.rowcount == 0:
            async with db.execute(
                "SELECT * FROM chat_coin_awards WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ) as cur:
                row = await cur.fetchone()
            return False, dict(row) if row else None
        coin = await db.execute(
            "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount, reason, awarded_by, stamp, SOURCE_CHAT),
        )
        await db.execute(
            "UPDATE chat_coin_awards SET coin_id = ? WHERE chat_id = ? AND message_id = ?",
            (coin.lastrowid, chat_id, message_id),
        )
        await db.commit()
    return True, None


async def transferred_user_ids() -> set[int]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT DISTINCT user_id FROM coins WHERE source = ?", (SOURCE_TRANSFER,)
        ) as cur:
            return {int(r[0]) for r in await cur.fetchall()}


async def record_transfer(entries: list[tuple[int, int, str]], changed_by: int) -> list[int]:
    """entries — (user_id, сумма, причина). Одной транзакцией; кто уже перенесён — пропуск.
    Возвращает id тех, кому начислено сейчас."""
    stamp = _stamp()
    done: list[int] = []
    async with _db._connect() as db:
        async with db.execute(
            "SELECT DISTINCT user_id FROM coins WHERE source = ?", (SOURCE_TRANSFER,)
        ) as cur:
            already = {int(r[0]) for r in await cur.fetchall()}
        for user_id, amount, reason in entries:
            if user_id in already or amount <= 0:
                continue
            await db.execute(
                "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, amount, reason, changed_by, stamp, SOURCE_TRANSFER),
            )
            already.add(user_id)
            done.append(user_id)
        await db.commit()
    return done


async def chat_username_ids(needles: list[str]) -> dict[str, int]:
    """@ник из чата (без «@», в нижнем регистре) -> telegram_id, только для тех, кто есть в
    `users`: монеты человеку без анкеты некуда показать."""
    if not needles:
        return {}
    out: dict[str, int] = {}
    async with _db._connect() as db:
        for needle in needles:
            async with db.execute(
                "SELECT cu.telegram_id FROM chat_usernames cu "
                "JOIN users u ON u.telegram_id = cu.telegram_id "
                "WHERE ltrim(cu.username, '@') = ? COLLATE NOCASE "
                "ORDER BY cu.updated_at DESC LIMIT 1",
                (needle,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                out[needle] = int(row[0])
    return out
