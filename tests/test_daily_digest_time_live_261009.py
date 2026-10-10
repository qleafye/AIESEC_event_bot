"""«📊 Итоги дня: во сколько» действует без перезапуска: запись ключа переносит cron-джобу."""
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import config
from services import daily_digest, scheduler
from settings_audit import set_setting_by_admin
from tests._dbtpl import fast_init_db


def test_digest_time_change_reschedules_job(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "digest_time.db")
    fast_init_db()

    async def _go():
        sched = AsyncIOScheduler()
        sched.add_job(daily_digest.daily_digest_job, "cron", hour=21, minute=0, id=daily_digest.JOB_ID)
        monkeypatch.setattr(scheduler, "_scheduler", sched)
        await set_setting_by_admin(1, "daily_digest_time", "18:30")
        trigger = sched.get_job(daily_digest.JOB_ID).trigger
        return {f.name: str(f) for f in trigger.fields}

    fields = asyncio.run(_go())
    assert fields["hour"] == "18" and fields["minute"] == "30"


def test_other_key_and_no_scheduler_are_noops(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "digest_time2.db")
    fast_init_db()
    monkeypatch.setattr(scheduler, "_scheduler", None)
    asyncio.run(set_setting_by_admin(1, "daily_digest_time", "07:15"))  # без планировщика — не падает
    asyncio.run(daily_digest.on_setting_written("start_text"))


def test_miniapp_outbox_runs_setting_hooks(monkeypatch):
    from services import miniapp_outbox
    import settings_audit

    seen = []

    async def _batch(keys):
        seen.append(list(keys))

    monkeypatch.setattr(settings_audit, "run_setting_hooks_batch", _batch)
    asyncio.run(miniapp_outbox._handle_row(None, "settings_changed", {"keys": ["daily_digest_time", "x"]}))
    assert seen == [["daily_digest_time", "x"]]  # одним вызовом на пакет, не по ключу
