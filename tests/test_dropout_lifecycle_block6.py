"""Block 6 reg_started lifecycle fixes: MD-02 (dropout queries exclude registered users) and
MD-03 (mark_reg_started preserves the original started_at on re-entry).

pytest-asyncio unavailable → async helpers driven via asyncio.run().
"""
import asyncio
import sqlite3

from config import config
from database import db


def _use_tmp(tmp_path):
    config.DB_PATH = str(tmp_path / "dropout.db")


def _set_started_at(tid, value):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE reg_started SET started_at = ? WHERE telegram_id = ?", (value, tid))
    conn.commit()
    conn.close()


def _read_started_at(tid):
    conn = sqlite3.connect(config.DB_PATH)
    row = conn.execute("SELECT started_at FROM reg_started WHERE telegram_id = ?", (tid,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_md03_mark_reg_started_preserves_started_at_on_reentry(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.mark_reg_started(1, "user1")
        _set_started_at(1, "2020-01-01 00:00:00")  # pretend this was long ago
        await db.mark_reg_started(1, "user1_renamed")  # re-entry / next step
        assert _read_started_at(1) == "2020-01-01 00:00:00"  # NOT reset to now
        # username still updates on re-entry
        row = await db.get_incomplete_rows()
        assert row and row[0][1] == "user1_renamed"

    asyncio.run(go())


def test_md02_dropout_queries_exclude_registered_keep_rejected(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        # tid 1: genuine dropout (reg_started, no users row)
        # tid 2: registered/approved — must be EXCLUDED from dropout views
        # tid 3: rejected re-registrant — must be KEPT (D-05a re-registration)
        await db.mark_reg_started(1, "dropout")
        await db.mark_reg_started(2, "approved")
        await db.mark_reg_started(3, "rejected")
        await db.add_user({"telegram_id": 2, "full_name": "A", "registration_date": "2026-07-01 10:00:00"})
        await db.set_user_status(2, "approved")
        await db.add_user({"telegram_id": 3, "full_name": "R", "registration_date": "2026-07-01 10:00:00"})
        await db.set_user_status(3, "rejected")

        ids = await db.get_incomplete_user_ids()
        assert set(ids) == {1, 3}  # approved (2) excluded, rejected (3) kept

        rows = {r[0] for r in await db.get_incomplete_rows()}
        assert rows == {1, 3}

        # Nudge candidates: push all started_at into the past, none nudged yet.
        for tid in (1, 2, 3):
            _set_started_at(tid, "2020-01-01 00:00:00")
        cands = await db.get_nudge_candidates("2099-01-01 00:00:00")
        assert set(cands) == {1, 3}

    asyncio.run(go())
