"""Block 6 scheduler fixes: ME-01 (timezone pin) + ME-03 (startup reconciliation).

Builds a real AsyncIOScheduler against a tmp jobstore so both the timezone and the
boot-time re-arm of a pending broadcast with a missing job are exercised end to end.
pytest-asyncio is unavailable, so the async lifecycle is driven via asyncio.run().
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from services import scheduler as sched


def _isolate(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "recon.db")
    # Point the APScheduler jobstore at a throwaway sqlite so we never touch data/jobs.sqlite.
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


def test_me01_scheduler_timezone_is_moscow(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            assert "Moscow" in str(s.timezone)  # ME-01: naive run_dates interpreted as MSK
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())


def test_me03_reconcile_rearms_pending_broadcast_with_missing_job(tmp_path, monkeypatch):
    """A 'pending' broadcast row whose bcast_<id> job is absent from the jobstore (the state a
    long downtime leaves) must be re-armed at startup. Future run_date keeps it from firing so
    the test asserts only the re-arm, not a live send."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        bid = await db.create_scheduled_broadcast("hi", None, None, future, created_by=1)

        s = await sched.init_scheduler(bot=object())  # init runs reconcile_scheduled_broadcasts
        try:
            # No one scheduled bcast_<bid> except reconcile — its presence proves the re-arm.
            assert s.get_job(f"bcast_{bid}") is not None
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())


def test_me02_mark_broadcast_sending_claims_once(tmp_path, monkeypatch):
    """ME-02: the pending→sending claim is atomic — the first caller gets rowcount 1, any
    second caller (re-fire / double schedule) gets 0 and must bail."""
    config.DB_PATH = str(tmp_path / "me02.db")

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast("x", None, None, "2026-07-01 14:30:00", created_by=1)
        assert await db.mark_broadcast_sending(bid) == 1  # first claim wins
        assert (await db.get_scheduled_broadcast(bid))["status"] == "sending"
        assert await db.mark_broadcast_sending(bid) == 0  # second claim rejected

    asyncio.run(go())


def test_me02_sending_broadcast_not_reconciled(tmp_path, monkeypatch):
    """A broadcast crashed mid-send (status='sending') must NOT be re-armed at boot — otherwise
    the whole audience would be blasted again. Only 'pending' rows reconcile."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        bid = await db.create_scheduled_broadcast("mid", None, None, past, created_by=1)
        await db.mark_broadcast_sending(bid)  # simulate a crash mid-send

        s = await sched.init_scheduler(bot=object())
        try:
            assert s.get_job(f"bcast_{bid}") is None  # 'sending' → never re-armed
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())


def test_me03_reconcile_skips_already_sent(tmp_path, monkeypatch):
    """A non-pending (sent) broadcast must NOT be re-armed."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        bid = await db.create_scheduled_broadcast("done", None, None, past, created_by=1)
        await db.mark_broadcast_sent(bid)

        s = await sched.init_scheduler(bot=object())
        try:
            assert s.get_job(f"bcast_{bid}") is None  # sent → not reconciled
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())
