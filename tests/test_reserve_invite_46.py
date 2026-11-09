"""Фаза 46: приглашение резерва на день, без смены status."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from services.forum import checkin, reserve


TID, STAFF = 461001, 1


def _run(coro):
    return asyncio.run(coro)


def _env(tmp_path):
    config.DB_PATH = str(tmp_path / "reserve46.db")
    _run(db.init_db())


async def _set_status(tid: int, status: str) -> None:
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, tid))
        await conn.commit()


def _rejected_auto():
    _run(db.add_user({
        "telegram_id": TID, "full_name": "Резерв Иван", "username": "@rezerv",
        "event_city": "msk", "registration_date": "2026-01-01",
    }))
    _run(_set_status(TID, "rejected"))
    _run(db.upsert_auto_reject_log(TID, "[1]", '["курс"]', "2026-01-01 12:00:00"))


def test_live_reserve_needs_log_and_rejected(tmp_path):
    _env(tmp_path)
    _run(db.add_user({
        "telegram_id": TID, "full_name": "Ручной",
        "event_city": "msk", "registration_date": "2026-01-01",
    }))
    _run(_set_status(TID, "rejected"))
    user = _run(db.get_user(TID))
    assert _run(reserve.is_live_reserve(user)) is False
    _run(db.upsert_auto_reject_log(TID, "[1]", '["x"]', "2026-01-01 12:00:00"))
    user = _run(db.get_user(TID))
    assert _run(reserve.is_live_reserve(user)) is True


def test_invite_idempotent_and_denial(tmp_path):
    _env(tmp_path)
    _rejected_auto()
    user = _run(db.get_user(TID))
    assert _run(checkin.checkin_denial(user)) == "reserve_not_invited"
    day = reserve.today_msk()
    assert _run(db.invite_reserve([TID], day, city="msk", invited_by=STAFF)) == 1
    assert _run(db.invite_reserve([TID], day, city="msk", invited_by=STAFF)) == 0
    user = _run(db.get_user(TID))
    assert _run(checkin.checkin_denial(user)) is None


def test_manual_reject_still_blocked(tmp_path):
    _env(tmp_path)
    _run(db.add_user({
        "telegram_id": TID, "full_name": "Ручной отказ",
        "event_city": "msk", "registration_date": "2026-01-01",
    }))
    _run(_set_status(TID, "rejected"))
    day = reserve.today_msk()
    _run(db.invite_reserve([TID], day, city="msk", invited_by=STAFF))
    user = _run(db.get_user(TID))
    assert _run(checkin.checkin_denial(user)) == "not_approved"


def test_approved_unchanged(tmp_path):
    _env(tmp_path)
    _run(db.add_user({
        "telegram_id": TID, "full_name": "Одобрен", "status": "approved",
        "event_city": "msk", "registration_date": "2026-01-01",
    }))
    user = _run(db.get_user(TID))
    assert _run(checkin.checkin_denial(user)) is None
