"""Закрытая ссылка чата города: политика ссылки, белый список сводки, пачка уведомления.

Состав чата здесь не хранится. Соединение — `database.db._connect()`
(тесты подменяют `config.DB_PATH`).
"""
from __future__ import annotations

import aiosqlite

from database import db as _db
from services.infra.timeutil import msk_now


def _city_key(city: str | None) -> str:
    return city or ""


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS city_chat_links ("
        "city TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, invite_link TEXT, "
        "creates_join_request INTEGER NOT NULL DEFAULT 1, expire_date INTEGER, "
        "member_limit INTEGER, lifetime_days INTEGER, updated_at TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS city_chat_allow ("
        "chat_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL, "
        "added_by INTEGER, added_at TEXT NOT NULL, "
        "PRIMARY KEY (chat_id, telegram_id))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS city_chat_summary_notice ("
        "chat_id INTEGER NOT NULL, admin_id INTEGER NOT NULL, "
        "message_id INTEGER NOT NULL, opened_at TEXT NOT NULL, "
        "PRIMARY KEY (chat_id, admin_id))"
    )


async def get_link(city: str | None) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT city, chat_id, invite_link, creates_join_request, expire_date, "
            "member_limit, lifetime_days, updated_at FROM city_chat_links WHERE city = ?",
            (_city_key(city),),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def save_link(
    city: str | None,
    *,
    chat_id: int,
    invite_link: str | None = None,
    creates_join_request: int = 1,
    expire_date: int | None = None,
    member_limit: int | None = None,
    lifetime_days: int | None = None,
) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO city_chat_links ("
            "city, chat_id, invite_link, creates_join_request, expire_date, "
            "member_limit, lifetime_days, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city) DO UPDATE SET "
            "chat_id = excluded.chat_id, invite_link = excluded.invite_link, "
            "creates_join_request = excluded.creates_join_request, "
            "expire_date = excluded.expire_date, member_limit = excluded.member_limit, "
            "lifetime_days = excluded.lifetime_days, updated_at = excluded.updated_at",
            (
                _city_key(city),
                int(chat_id),
                invite_link,
                1 if creates_join_request else 0,
                expire_date,
                member_limit,
                lifetime_days,
                _stamp(),
            ),
        )
        await db.commit()


async def allow(chat_id: int, telegram_id: int, added_by: int | None) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO city_chat_allow (chat_id, telegram_id, added_by, added_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, telegram_id) DO UPDATE SET "
            "added_by = excluded.added_by, added_at = excluded.added_at",
            (int(chat_id), int(telegram_id), added_by, _stamp()),
        )
        await db.commit()


async def allowed_ids(chat_id: int) -> set[int]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM city_chat_allow WHERE chat_id = ?",
            (int(chat_id),),
        ) as cur:
            rows = await cur.fetchall()
    return {int(row[0]) for row in rows}


async def is_allowed(chat_id: int, telegram_id: int) -> bool:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT 1 FROM city_chat_allow WHERE chat_id = ? AND telegram_id = ?",
            (int(chat_id), int(telegram_id)),
        ) as cur:
            return await cur.fetchone() is not None


async def summary_notices(chat_id: int) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT chat_id, admin_id, message_id, opened_at "
            "FROM city_chat_summary_notice WHERE chat_id = ? ORDER BY admin_id",
            (int(chat_id),),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def summary_notice(chat_id: int, admin_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT chat_id, admin_id, message_id, opened_at "
            "FROM city_chat_summary_notice WHERE chat_id = ? AND admin_id = ?",
            (int(chat_id), int(admin_id)),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def save_summary_notice(chat_id: int, admin_id: int, message_id: int) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO city_chat_summary_notice (chat_id, admin_id, message_id, opened_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, admin_id) DO UPDATE SET "
            "message_id = excluded.message_id, opened_at = excluded.opened_at",
            (int(chat_id), int(admin_id), int(message_id), _stamp()),
        )
        await db.commit()


async def clear_summary_notice(chat_id: int, admin_id: int) -> None:
    async with _db._connect() as db:
        await db.execute(
            "DELETE FROM city_chat_summary_notice WHERE chat_id = ? AND admin_id = ?",
            (int(chat_id), int(admin_id)),
        )
        await db.commit()
