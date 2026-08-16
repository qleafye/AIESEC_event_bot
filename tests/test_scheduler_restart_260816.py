"""Night review 260816 (review/services.md #2 и #4): планировщик переживает рестарт.

#2 — `init_scheduler` регистрировал четыре interval-джобы с `replace_existing=True` и без
`next_run_time`. APScheduler в этом случае считает `next_run_time = trigger.get_next_fire_time(None, now)`,
а `IntervalTrigger` без `start_date` берёт `now + interval` (triggers/interval.py:69), и
`update_job` затирает сохранённое в jobstore расписание (schedulers/base.py:1075-1080).
Итог: каждый рестарт отодвигал следующий прогон на boot+интервал, и при `restart: always`
24-часовой `sweep_payment_overdue` не выполнялся никогда.

#4 — `reconcile_scheduled_broadcasts` ре-армил `pending`-рассылку её же прошедшей датой.
Если дата старше `misfire_grace_time` (86400 с), executor выбрасывает run как misfire
(executors/base.py:117-127) и date-джоба удаляется — строка остаётся `pending` навсегда.

pytest-asyncio в проекте нет: весь async-жизненный цикл гоняется через `asyncio.run()`.
Живой `data/jobs.sqlite` не трогается — `_JOBSTORE_URL` монкипатчится на `tmp_path`
(конвенция из tests/test_scheduler_reconcile_block6.py).

ВАЖНО про детерминизм: ассерты по `next_run_time` делаются СРАЗУ после
`await init_scheduler(...)`, без единого `await` между ними. `resume()` планирует разбор
джоб через `call_soon_threadsafe`, поэтому без точки передачи управления цикл событий его
не выполнит и джобы гарантированно не сработают во время проверки.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database import db
from services import scheduler as sched
from services.scheduler import MOSCOW_TZ


def _isolate(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "restart.db")
    # Point the APScheduler jobstore at a throwaway sqlite so we never touch data/jobs.sqlite.
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


async def _restart(s):
    """Simulate a process restart: stop the live scheduler, drop the module global.

    `AsyncIOScheduler.shutdown` is itself deferred via `call_soon_threadsafe`, so a single
    `sleep(0)` is required for it to actually run before the next scheduler opens the store.
    """
    s.pause()  # a queued wakeup must not process (and re-stamp) jobs while we tear down
    s.shutdown(wait=False)
    await asyncio.sleep(0)
    sched._scheduler = None


class _FakeJob:
    def __init__(self, trigger):
        self.trigger = trigger


# ── #2: pure predicate ───────────────────────────────────────────────────────────────────

def test_interval_matches_is_a_pure_predicate():
    fifteen = timedelta(minutes=15)

    assert sched._interval_matches(_FakeJob(IntervalTrigger(seconds=900)), fifteen) is True
    assert sched._interval_matches(_FakeJob(IntervalTrigger(seconds=1800)), fifteen) is False
    assert sched._interval_matches(_FakeJob(DateTrigger()), fifteen) is False
    assert sched._interval_matches(None, fifteen) is False
    assert sched._interval_matches(_FakeJob(None), fifteen) is False


# ── #2: registration survives the reordered lifecycle ────────────────────────────────────

def test_all_four_interval_jobs_registered(tmp_path, monkeypatch):
    """start(paused=True) → add_job ×4 → reconcile → resume: nothing lost in the reorder."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            ids = {j.id for j in s.get_jobs()}
            assert {
                "nudge_scan", "allowlist_refresh", "payment_overdue_sweep", "incomplete_sheet_sync",
            } <= ids
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())


def test_stored_future_schedule_survives_restart(tmp_path, monkeypatch):
    """services.md #2: the saved next_run_time of the 24h sweep must NOT be pushed to boot+24h.

    A bot restarted more often than once a day never ran the sweep at all: payment_status
    never flipped to 'overdue' and the «неоплатившие» broadcast segment stayed empty.
    """
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        s1 = await sched.init_scheduler(bot=object())
        s1.pause()
        saved = datetime.now(MOSCOW_TZ) + timedelta(hours=7)
        s1.modify_job("payment_overdue_sweep", next_run_time=saved)
        s1.shutdown(wait=False)
        await asyncio.sleep(0)
        sched._scheduler = None

        s2 = await sched.init_scheduler(bot=object())
        after = s2.get_job("payment_overdue_sweep").next_run_time
        try:
            assert after is not None
            assert abs((after - saved).total_seconds()) <= 1, (
                f"расписание сброшено рестартом: было {saved}, стало {after}"
            )
        finally:
            s2.shutdown(wait=False)

    asyncio.run(go())


def test_stored_past_schedule_gets_boot_catchup(tmp_path, monkeypatch):
    """A saved run time that already passed (the bot was down) must fire shortly after boot.

    Feeding the past time straight back would be dropped as a misfire for anything older than
    `misfire_grace_time`, i.e. exactly the silent loss this fix is about.
    """
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        s1 = await sched.init_scheduler(bot=object())
        s1.pause()
        s1.modify_job(
            "payment_overdue_sweep",
            next_run_time=datetime.now(MOSCOW_TZ) - timedelta(days=2),
        )
        s1.shutdown(wait=False)
        await asyncio.sleep(0)
        sched._scheduler = None

        s2 = await sched.init_scheduler(bot=object())
        after = s2.get_job("payment_overdue_sweep").next_run_time
        now = datetime.now(MOSCOW_TZ)
        try:
            assert after > now, "просроченная джоба обязана получить время в будущем"
            assert after <= now + timedelta(minutes=5), (
                f"догоняющий прогон должен быть вскоре после старта, а не через сутки: {after}"
            )
        finally:
            s2.shutdown(wait=False)

    asyncio.run(go())


def test_changed_interval_recomputes_schedule(tmp_path, monkeypatch):
    """Менеджер сменил интервал в настройках — расписание обязано пересчитаться, а не
    «сохраниться» ради находки #2."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        s1 = await sched.init_scheduler(bot=object())
        before = s1.get_job("nudge_scan").next_run_time  # default 15 min
        await _restart(s1)

        await db.set_setting("nudge_scan_minutes", "30")
        s2 = await sched.init_scheduler(bot=object())
        after = s2.get_job("nudge_scan").next_run_time
        expected = datetime.now(MOSCOW_TZ) + timedelta(minutes=30)
        try:
            assert after > before + timedelta(minutes=10), (
                f"смена интервала 15→30 не применилась: было {before}, стало {after}"
            )
            assert abs((after - expected).total_seconds()) <= 60
        finally:
            s2.shutdown(wait=False)

    asyncio.run(go())


# ── #4: a pending broadcast older than the misfire grace must go out late, not vanish ─────

def _stop(s):
    """Pause before shutting down: `resume()` queued a wakeup via call_soon_threadsafe, and a
    re-armed job whose run time already passed would otherwise fire while the loop unwinds."""
    s.pause()
    s.shutdown(wait=False)


def test_stale_pending_broadcast_is_rearmed_into_the_future(tmp_path, monkeypatch):
    """services.md #4: re-arming a >24h-old row with its own past date fed the executor a run
    older than `misfire_grace_time`, which drops it (executors/base.py:117-127). The date job
    disappeared, the row stayed 'pending' forever, and every boot repeated the same silent
    drop. The docstring promised a LATE send — this makes the promise true."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        stale = sched._fmt_dt(sched._now_moscow_naive() - timedelta(days=3))
        bid = await db.create_scheduled_broadcast("stale", None, None, stale, created_by=1)

        s = await sched.init_scheduler(bot=object())
        job = s.get_job(f"bcast_{bid}")
        now = datetime.now(MOSCOW_TZ)
        try:
            assert job is not None, "просроченная pending-рассылка обязана быть ре-армлена"
            assert job.next_run_time > now, (
                f"run_date остался в прошлом ({job.next_run_time}) — executor снова дропнет его "
                f"как misfire, рассылка молча пропадёт"
            )
            assert job.next_run_time <= now + timedelta(minutes=10)
        finally:
            _stop(s)

    asyncio.run(go())


def test_pending_broadcast_inside_grace_keeps_its_original_run_date(tmp_path, monkeypatch):
    """Within the grace the executor still fires the stored time — do not move it."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        run_at = (sched._now_moscow_naive() - timedelta(hours=1)).replace(microsecond=0)
        bid = await db.create_scheduled_broadcast(
            "recent", None, None, sched._fmt_dt(run_at), created_by=1
        )

        s = await sched.init_scheduler(bot=object())
        job = s.get_job(f"bcast_{bid}")
        try:
            assert job is not None
            # A naive run_date is localized into the scheduler timezone (MOSCOW_TZ).
            assert job.next_run_time == run_at.replace(tzinfo=MOSCOW_TZ)
        finally:
            _stop(s)

    asyncio.run(go())


def test_stale_rearm_does_not_rewrite_the_db_row(tmp_path, monkeypatch):
    """Экран /scheduled (handlers/admin.py:2373) печатает scheduled_at прямо из БД — менеджер
    обязан видеть введённое им время, поэтому переносится только run_date джобы."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        stale = sched._fmt_dt(sched._now_moscow_naive() - timedelta(days=3))
        bid = await db.create_scheduled_broadcast("stale", None, None, stale, created_by=1)

        s = await sched.init_scheduler(bot=object())
        try:
            row = await db.get_scheduled_broadcast(bid)
            assert row["scheduled_at"] == stale
            assert row["status"] == "pending"
        finally:
            _stop(s)

    asyncio.run(go())


def test_stale_rearm_is_logged_with_the_original_time(tmp_path, monkeypatch, caplog):
    """A silently-lost broadcast becoming a late one is a deliberate, loud event."""
    _isolate(tmp_path, monkeypatch)
    stale = sched._fmt_dt(sched._now_moscow_naive() - timedelta(days=3))

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast("stale", None, None, stale, created_by=1)

        s = await sched.init_scheduler(bot=object())
        try:
            return bid
        finally:
            _stop(s)

    with caplog.at_level(logging.WARNING, logger="services.scheduler"):
        bid = asyncio.run(go())

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    # id рассылки И её исходное время — чтобы по логу было видно, что именно уехало и почему.
    assert any(f"broadcast {bid}" in m and stale in m for m in warnings), (
        f"перенос просроченной рассылки не залогирован WARNING'ом: {warnings}"
    )
