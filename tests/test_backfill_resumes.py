"""Unit test for scripts.backfill_resumes candidate selection.

pytest-asyncio is unavailable here, so we drive the async helpers via
asyncio.run() and point config.DB_PATH at a tmp_path file — same pattern as
tests/test_db_phase4.py. select_pending_resumes is imported directly and runs
against a plain aiosqlite connection, with no Bot construction.
"""
import asyncio

import aiosqlite

from config import config
from database import db
from scripts.backfill_resumes import select_pending_resumes


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


def test_select_pending_resumes_picks_only_missing(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    # (a) has file, no url  -> PICKED
    _seed(1, resume_file_id="FILE_A")
    # (b) has file AND url  -> SKIPPED (already linked)
    _seed(2, resume_file_id="FILE_B", resume_url="https://cloud/x")
    # (c) no file at all     -> SKIPPED
    _seed(3)

    async def _run():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            return await select_pending_resumes(conn)

    rows = asyncio.run(_run())

    assert len(rows) == 1
    telegram_id, resume_file_id = rows[0]
    assert telegram_id == 1
    assert resume_file_id == "FILE_A"
