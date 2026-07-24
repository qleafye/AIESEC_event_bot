"""Phase 4 + YL'26 data-layer tests.

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


# ── migrations: new columns exist, defaults sane ─────────────────────────────

def test_phase4_columns_added(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(config.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    for c in (
        "payment_status", "payment_option", "receipt_file_id", "payment_due", "paid_at",
        "birth_date", "study_field", "goal", "formats",
    ):
        assert c in cols, f"missing column {c}"


def test_migration_idempotent_no_data_loss(tmp_path):
    _use_tmp_db(tmp_path)
    # OLD schema: minimal users table + one row without Phase 4 columns
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT, status TEXT)")
    conn.execute("INSERT INTO users (telegram_id, full_name, status) VALUES (99, 'Old User', 'approved')")
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())
    asyncio.run(db.init_db())  # twice — idempotent

    u = asyncio.run(db.get_user(99))
    assert u["full_name"] == "Old User"
    assert u["status"] == "approved"          # preserved
    assert u["payment_status"] == "not_paid"  # backfilled by DEFAULT


def test_user_consents_table_and_index(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(config.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "user_consents" in tables
    assert "idx_consents_user" in idx


# ── new YL'26 fields roundtrip through add_user/get_user ──────────────────────

def test_yl26_fields_roundtrip(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(
        1,
        birth_date="01.01.2000",
        study_field="IT и технологии",
        goal="Нетворкинг, Узнать о деятельности компаний",
        formats="Мастер-классы",
        is_ambassador_candidate=True,
    )
    u = asyncio.run(db.get_user(1))
    assert u["birth_date"] == "01.01.2000"
    assert u["study_field"] == "IT и технологии"
    assert u["goal"].startswith("Нетворкинг")
    assert u["formats"] == "Мастер-классы"
    assert u["is_ambassador_candidate"] == 1


def test_reregistration_preserves_payment_state(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="FID"))
    # re-register same user (e.g. a rejected user re-applying)
    _seed(1, full_name="User 1 v2")
    u = asyncio.run(db.get_user(1))
    assert u["full_name"] == "User 1 v2"
    assert u["receipt_file_id"] == "FID"          # COALESCE keeps prior file
    # WR-06: payment state is owned by the payment flow and must NOT be wiped by
    # re-registration — add_user no longer touches payment_status on conflict.
    assert u["payment_status"] == "receipt_sent"


# ── consent helpers ──────────────────────────────────────────────────────────

def test_record_user_consent_idempotent(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.record_user_consent(1, "data"))
    asyncio.run(db.record_user_consent(1, "data"))  # no UNIQUE error
    asyncio.run(db.record_user_consent(1, "policy"))
    got = asyncio.run(db.get_user_consents(1))
    assert got == ["data", "policy"]


# ── payment status helpers + atomic confirm guard ────────────────────────────

def test_receipt_pending_queue_and_count(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)
    _seed(2)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="F1"))
    pending = asyncio.run(db.get_receipt_pending_users())
    assert [p["telegram_id"] for p in pending] == [1]
    assert pending[0]["receipt_file_id"] == "F1"
    assert asyncio.run(db.get_receipt_pending_count()) == 1


def test_update_payment_status_atomic_confirm_guard(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="F1"))
    # first confirm flips receipt_sent → paid
    assert asyncio.run(db.update_payment_status(1, "paid")) == 1
    # second confirm matches 0 rows (already paid) — double-confirm guard
    assert asyncio.run(db.update_payment_status(1, "paid")) == 0
    u = asyncio.run(db.get_user(1))
    assert u["payment_status"] == "paid"
    assert u["paid_at"]  # auto-stamped


def test_update_payment_status_reject_resets_unconditionally(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="F1"))
    assert asyncio.run(db.update_payment_status(1, "not_paid")) == 1
    assert asyncio.run(db.get_user(1))["payment_status"] == "not_paid"


def test_update_payment_status_reject_guard_blocks_paid_row(tmp_path):
    """H-01: a guarded reject (require_status='receipt_sent') must NOT un-pay a paid user.
    Mirrors the stale/already-confirmed card tapped ❌ Отклонить scenario."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="F1"))
    assert asyncio.run(db.update_payment_status(1, "paid")) == 1  # confirmed
    # Stale card ❌: guarded reject sees payment_status='paid' != 'receipt_sent' → no-op.
    assert asyncio.run(db.update_payment_status(1, "not_paid", require_status="receipt_sent")) == 0
    assert asyncio.run(db.get_user(1))["payment_status"] == "paid"  # unchanged


def test_update_payment_status_reject_guard_allows_receipt_sent_row(tmp_path):
    """H-01: a guarded reject still fires normally on a genuinely pending (receipt_sent) row."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="F1"))
    assert asyncio.run(db.update_payment_status(1, "not_paid", require_status="receipt_sent")) == 1
    assert asyncio.run(db.get_user(1))["payment_status"] == "not_paid"
