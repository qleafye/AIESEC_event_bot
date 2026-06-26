"""Phase 2 data-layer tests (approval flow).

pytest-asyncio is unavailable in this env, so each test drives the async db
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file.
"""
import asyncio

from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _seed(telegram_id, status, reg_date):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": reg_date,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))


# ── atomic approve / reject guards ───────────────────────────────────────────

def test_approve_user_atomic_wins_once(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(1, "pending", "2025-01-01")

    assert asyncio.run(db.approve_user_atomic(1)) is True
    # second approve on the now-approved row loses the race
    assert asyncio.run(db.approve_user_atomic(1)) is False
    assert asyncio.run(db.get_user(1))["status"] == "approved"


def test_reject_user_only_flips_pending(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(2, "pending", "2025-01-01")
    _seed(3, "approved", "2025-01-01")

    assert asyncio.run(db.reject_user(2)) is True
    assert asyncio.run(db.get_user(2))["status"] == "rejected"
    # rejecting an already-approved row is a no-op
    assert asyncio.run(db.reject_user(3)) is False
    assert asyncio.run(db.get_user(3))["status"] == "approved"


# ── pending queue + count ────────────────────────────────────────────────────

def test_pending_queue_order_and_count(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(10, "pending", "2025-03-03 09:00:00")
    _seed(11, "pending", "2025-01-01 09:00:00")  # oldest
    _seed(12, "pending", "2025-02-02 09:00:00")
    _seed(13, "approved", "2025-01-01 09:00:00")
    _seed(14, "rejected", "2025-01-01 09:00:00")

    assert asyncio.run(db.get_pending_count()) == 3

    oldest = asyncio.run(db.get_pending_users(limit=1))
    assert len(oldest) == 1 and oldest[0]["telegram_id"] == 11

    all_pending = asyncio.run(db.get_pending_users(limit=10))
    assert [u["telegram_id"] for u in all_pending] == [11, 12, 10]


# ── bulk approve all ─────────────────────────────────────────────────────────

def test_conference_fields_roundtrip(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_user({
        "telegram_id": 99,
        "full_name": "Конф Юзер",
        "registration_date": "2026-03-01",
        "department": "BD",
        "aiesec_role": "TL",
        "needs_certificate": "Нет",
        "english_level": "Средний",
        "arrival": "В дни конфы",
        "housing": "Хост",
        "volunteer": "Да",
    }))
    u = asyncio.run(db.get_user(99))
    assert u["department"] == "BD"
    assert u["aiesec_role"] == "TL"
    assert u["english_level"] == "Средний"
    assert u["arrival"] == "В дни конфы"
    assert u["volunteer"] == "Да"


def test_approve_all_pending_flips_once(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed(20, "pending", "2025-01-01")
    _seed(21, "pending", "2025-01-02")
    _seed(22, "pending", "2025-01-03")
    _seed(23, "approved", "2025-01-01")

    ids = asyncio.run(db.approve_all_pending())
    assert sorted(ids) == [20, 21, 22]
    assert asyncio.run(db.get_pending_count()) == 0
    # second call returns nothing — no id welcomed twice
    assert asyncio.run(db.approve_all_pending()) == []
    # pre-existing approved row untouched
    assert asyncio.run(db.get_user(23))["status"] == "approved"
