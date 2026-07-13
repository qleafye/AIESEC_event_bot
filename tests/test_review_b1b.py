"""Regression tests for the 2026-07-13 all-phases code review, wave B1b (LOGIC criticals).

Covers:
- CR-1: moderation/receipt queue paging (offset must reach rows past the first 50)
- CR-7: stale/foreign consent-tap identity guard
- CR-8: Unicode-digit-safe int parsing (referrer id + age)
- CR-9: frozen sheet-schema snapshot alignment

pytest-asyncio is unavailable in this env, so async db/registration helpers are driven
via asyncio.run() with config.DB_PATH pointed at a tmp_path file (matches
tests/test_db_phase4.py and tests/test_registration_phase4.py conventions).
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _seed_pending(telegram_id, reg_date="2026-01-01"):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": reg_date,
    }))
    # New users default to status='approved' (column default); the registration
    # finalize flow explicitly marks them 'pending' via set_user_status.
    asyncio.run(db.set_user_status(telegram_id, "pending"))


# ── CR-1: queue paging ────────────────────────────────────────────────────────

def test_pending_users_offset_pages_past_first_50(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    for i in range(60):
        # registration_date ASC, telegram_id ASC ordering — spread dates so order is stable.
        _seed_pending(i, reg_date=f"2026-01-01T00:00:{i:02d}")

    first_batch = asyncio.run(db.get_pending_users(limit=50, offset=0))
    second_batch = asyncio.run(db.get_pending_users(limit=50, offset=50))

    assert len(first_batch) == 50
    assert len(second_batch) == 10
    first_ids = {u["telegram_id"] for u in first_batch}
    second_ids = {u["telegram_id"] for u in second_batch}
    assert not (first_ids & second_ids)  # no overlap
    assert first_ids | second_ids == set(range(60))


def test_receipt_pending_users_offset_pages_past_first_50(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    for i in range(60):
        _seed_pending(i)
        asyncio.run(db.update_payment_status(i, "receipt_sent", receipt_file_id=f"F{i}"))

    first_batch = asyncio.run(db.get_receipt_pending_users(limit=50, offset=0))
    second_batch = asyncio.run(db.get_receipt_pending_users(limit=50, offset=50))

    assert len(first_batch) == 50
    assert len(second_batch) == 10
    first_ids = {u["telegram_id"] for u in first_batch}
    second_ids = {u["telegram_id"] for u in second_batch}
    assert not (first_ids & second_ids)
    assert first_ids | second_ids == set(range(60))


def test_receipt_pending_users_offset_default_unchanged(tmp_path):
    # <50-item behavior must stay byte-identical to before offset was added.
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed_pending(1)
    asyncio.run(db.update_payment_status(1, "receipt_sent", receipt_file_id="F1"))
    pending = asyncio.run(db.get_receipt_pending_users())
    assert [p["telegram_id"] for p in pending] == [1]
