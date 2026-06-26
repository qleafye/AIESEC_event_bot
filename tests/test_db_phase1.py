"""Phase 1 data-layer tests.

pytest-asyncio is unavailable in this env, so each test drives the async db
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file.
"""
import asyncio
import sqlite3

from config import config
from database import db


def _use_tmp_db(tmp_path):
    db_file = tmp_path / "test_forum.db"
    config.DB_PATH = str(db_file)
    return db_file


# ── Migrations ───────────────────────────────────────────────────────────────

def test_migration_preserves_existing_users_with_approved_status(tmp_path):
    db_file = _use_tmp_db(tmp_path)
    # Seed a minimal OLD-schema users table (no status column) + one row.
    conn = sqlite3.connect(db_file)
    conn.execute('''
        CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY,
            full_name TEXT,
            registration_date TEXT
        )
    ''')
    conn.execute(
        "INSERT INTO users (telegram_id, full_name, registration_date) VALUES (?, ?, ?)",
        (111, "Existing User", "2025-01-01"),
    )
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())

    conn = sqlite3.connect(db_file)
    row = conn.execute("SELECT status FROM users WHERE telegram_id = 111").fetchone()
    conn.close()
    assert row[0] == "approved"


def test_add_user_on_conflict_preserves_status_and_resume(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    asyncio.run(db.add_user({
        "telegram_id": 222,
        "full_name": "First Name",
        "registration_date": "2025-02-01",
    }))

    # Simulate the approval flow flipping status + a stored resume.
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE users SET status = 'pending', resume_file_id = 'abc123' WHERE telegram_id = 222"
    )
    conn.commit()
    conn.close()

    # Re-register (same telegram_id) with a changed name — must NOT reset status.
    asyncio.run(db.add_user({
        "telegram_id": 222,
        "full_name": "Changed Name",
        "registration_date": "2025-02-01",
    }))

    user = asyncio.run(db.get_user(222))
    assert user["status"] == "pending"          # status untouched by re-registration
    assert user["resume_file_id"] == "abc123"   # resume round-trips (not wiped by REPLACE)
    assert user["full_name"] == "Changed Name"  # mutable field updated


def test_add_user_persists_resume_file_id(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_user({
        "telegram_id": 223,
        "full_name": "Resume User",
        "registration_date": "2025-02-02",
        "resume_file_id": "FILEID_XYZ",
    }))
    user = asyncio.run(db.get_user(223))
    assert user["resume_file_id"] == "FILEID_XYZ"


# ── Coins ledger ─────────────────────────────────────────────────────────────

def test_balance_is_sum_of_deltas(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_coins(333, 10))
    asyncio.run(db.add_coins(333, -3))
    assert asyncio.run(db.get_balance(333)) == 7


def test_negative_balance_allowed(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_coins(444, -100))
    assert asyncio.run(db.get_balance(444)) == -100


def test_balance_zero_for_unknown_user(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_balance(999)) == 0


def test_leaderboard_ordering_and_rank(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_coins(1, 50))
    asyncio.run(db.add_coins(2, 30))
    asyncio.run(db.add_coins(3, 90))

    board = asyncio.run(db.get_leaderboard(10))
    assert [r["user_id"] for r in board] == [3, 1, 2]
    assert board[0]["balance"] == 90

    assert asyncio.run(db.get_user_rank(3)) == 1
    assert asyncio.run(db.get_user_rank(1)) == 2
    assert asyncio.run(db.get_user_rank(2)) == 3
    assert asyncio.run(db.get_user_rank(999)) is None


# ── reg_started lifecycle ────────────────────────────────────────────────────

def test_reg_started_mark_query_clear(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.mark_reg_started(555, "user555"))
    assert 555 in asyncio.run(db.get_incomplete_user_ids())
    asyncio.run(db.clear_reg_started(555))
    assert 555 not in asyncio.run(db.get_incomplete_user_ids())


def test_reg_started_double_mark_upserts(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.mark_reg_started(666, "old_name"))
    asyncio.run(db.mark_reg_started(666, "new_name"))

    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT username FROM reg_started WHERE telegram_id = 666"
    ).fetchall()
    conn.close()
    assert len(rows) == 1            # exactly one row (upsert, not duplicate)
    assert rows[0][0] == "new_name"  # username updated


# ── Subscription flag ────────────────────────────────────────────────────────

def test_subscription_flag_set_and_query(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_user({
        "telegram_id": 777,
        "full_name": "Sub User",
        "registration_date": "2025-03-01",
    }))
    asyncio.run(db.set_user_subscribed(777, False))
    assert 777 in asyncio.run(db.get_non_subscriber_ids())
    asyncio.run(db.set_user_subscribed(777, True))
    assert 777 not in asyncio.run(db.get_non_subscriber_ids())
