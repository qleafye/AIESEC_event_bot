"""Phase 5 (Participant Tracks) data-layer tests.

pytest-asyncio is unavailable in this env, so each test drives the async db
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file.
"""
import asyncio
import sqlite3

from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _seed(telegram_id, reg_date="2026-01-01", **extra):
    payload = {
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": reg_date,
    }
    payload.update(extra)
    asyncio.run(db.add_user(payload))


# ── migrations: new columns exist, defaults sane (D-01) ──────────────────────

def test_phase5_columns_added(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(config.DB_PATH)
    users_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    reg_started_cols = {row[1] for row in conn.execute("PRAGMA table_info(reg_started)")}
    conn.close()
    assert "participant_type" in users_cols
    assert "participant_type" in reg_started_cols


def test_migration_idempotent_no_data_loss_default_full(tmp_path):
    """D-01: init_db() run twice against a DB holding pre-existing rows leaves every
    row readable with participant_type == 'full' — zero data loss."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)  # row written before we simulate a "pre-migration" re-run
    asyncio.run(db.init_db())  # idempotent re-run must not raise or clobber data
    u = asyncio.run(db.get_user(1))
    assert u["participant_type"] == "full"
    assert u["full_name"] == "User 1"


def test_migration_against_old_schema_row_backfills_full(tmp_path):
    """Pre-migration-rows case: an OLD schema row (created before this migration existed)
    reads back participant_type == 'full' after init_db(), zero data loss."""
    _use_tmp_db(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT, status TEXT)")
    conn.execute("INSERT INTO users (telegram_id, full_name, status) VALUES (99, 'Old User', 'approved')")
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())
    asyncio.run(db.init_db())  # twice — idempotent

    u = asyncio.run(db.get_user(99))
    assert u["full_name"] == "Old User"
    assert u["participant_type"] == "full"  # backfilled by DEFAULT, zero data loss


# ── mark_reg_started / get_reg_started_track (D-02) ───────────────────────────

def test_mark_reg_started_coalesce_preserves_track_on_bare_repeat(tmp_path):
    """A bare repeat /start (no deep-link arg) must NOT clobber the track recorded
    on the first call — COALESCE(excluded.x, table.x) guard."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.mark_reg_started(1, "user1", "party_overnight"))
    asyncio.run(db.mark_reg_started(1, "user1"))  # no track passed
    assert asyncio.run(db.get_reg_started_track(1)) == "party_overnight"


def test_mark_reg_started_fresh_explicit_track_wins(tmp_path):
    """A fresh explicit deep link overwrites a previously stored track."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.mark_reg_started(1, "user1", "party_overnight"))
    asyncio.run(db.mark_reg_started(1, "user1", "party_noovernight"))
    assert asyncio.run(db.get_reg_started_track(1)) == "party_noovernight"


def test_get_reg_started_track_unknown_returns_none(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_reg_started_track(999999)) is None


# ── add_user round-trip (D-01) ────────────────────────────────────────────────

def test_add_user_roundtrips_participant_type(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1, participant_type="party_noovernight")
    u = asyncio.run(db.get_user(1))
    assert u["participant_type"] == "party_noovernight"


def test_add_user_no_participant_type_key_defaults_full(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)  # no participant_type key at all
    u = asyncio.run(db.get_user(1))
    assert u["participant_type"] == "full"


# ── two-write-point contract (D-02, Task 3) ───────────────────────────────────

def test_two_write_point_contract_reg_started_then_users(tmp_path):
    """mark_reg_started (flow start) -> get_reg_started_track reads it -> add_user
    (finalize) persists it to users -> clear_reg_started deletes the reg_started row ->
    get_user still reports the track, get_reg_started_track returns None."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    asyncio.run(db.mark_reg_started(1, "user1", "party_overnight"))
    assert asyncio.run(db.get_reg_started_track(1)) == "party_overnight"

    _seed(1, participant_type="party_overnight")

    asyncio.run(db.clear_reg_started(1))

    u = asyncio.run(db.get_user(1))
    assert u["participant_type"] == "party_overnight"
    assert asyncio.run(db.get_reg_started_track(1)) is None
