"""init_db() must create the indexes under the hot admin/scheduler queries (moderation
queue, receipt pagination, scheduled broadcasts, dropout nudge, referrals, game queue).
Each index mirrors a real WHERE/ORDER BY in database/db.py; the test also checks that the
query planner actually picks them for the moderation and broadcast queries."""
import asyncio

import aiosqlite

from config import config
from database import db


def _init(tmp_path) -> str:
    path = str(tmp_path / "idx_test.db")
    config.DB_PATH = path
    asyncio.run(db.init_db())
    return path


async def _indexes(path) -> dict[str, str]:
    async with aiosqlite.connect(path) as conn:
        async with conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ) as cur:
            return {name: tbl for name, tbl in await cur.fetchall()}


def test_hot_path_indexes_exist_after_init(tmp_path):
    path = _init(tmp_path)
    existing = asyncio.run(_indexes(path))
    for name in db._HOT_PATH_INDEXES:
        assert name in existing, f"missing index {name}"
    assert existing["idx_users_status_city_regdate"] == "users"
    assert existing["idx_users_payment_status"] == "users"
    assert existing["idx_users_referrer"] == "users"
    assert existing["idx_users_subscribed"] == "users"
    assert existing["idx_scheduled_broadcasts_status_at"] == "scheduled_broadcasts"
    assert existing["idx_reg_started_started_at"] == "reg_started"
    assert existing["idx_reg_started_nudge"] == "reg_started"
    assert existing["idx_game_submissions_status_at"] == "game_submissions"


def test_init_db_is_idempotent_with_indexes(tmp_path):
    path = _init(tmp_path)
    asyncio.run(db.init_db())  # second run on the same file must not raise
    assert set(db._HOT_PATH_INDEXES) <= set(asyncio.run(_indexes(path)))


def test_planner_uses_indexes_for_hot_queries(tmp_path):
    path = _init(tmp_path)

    async def _plan(sql, params=()):
        async with aiosqlite.connect(path) as conn:
            async with conn.execute("EXPLAIN QUERY PLAN " + sql, params) as cur:
                return " | ".join(str(r) for r in await cur.fetchall())

    plan = asyncio.run(_plan(
        "SELECT * FROM users WHERE status = 'pending' AND event_city = ? "
        "ORDER BY registration_date ASC, telegram_id ASC LIMIT 1 OFFSET 0", ("msk",)))
    assert "idx_users_status_city_regdate" in plan, plan

    plan = asyncio.run(_plan(
        "SELECT * FROM scheduled_broadcasts WHERE status = 'pending' ORDER BY scheduled_at"))
    assert "idx_scheduled_broadcasts_status_at" in plan, plan

    plan = asyncio.run(_plan(
        "SELECT telegram_id FROM users WHERE payment_status = 'receipt_sent' ORDER BY rowid LIMIT 50"))
    assert "idx_users_payment_status" in plan, plan


def test_index_skipped_when_column_missing_in_legacy_schema(tmp_path):
    """A pre-migration users table without registration_date must not break init_db; the
    index that needs the column is skipped, the rest are still built."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    config.DB_PATH = path
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT, status TEXT)")
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())
    existing = asyncio.run(_indexes(path))
    assert "idx_users_status_city_regdate" not in existing
    assert "idx_users_payment_status" in existing  # payment_status is _ensure_column'd
    assert "idx_scheduled_broadcasts_status_at" in existing
