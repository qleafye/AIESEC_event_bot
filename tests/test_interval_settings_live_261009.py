"""Интервалы фоновых джоб (догонялка, предотбор, «Незавершённые»/автоотказы, повтор
выгрузки резюме, сверка чата) действуют без перезапуска: запись ключа перепланирует джобу.

Тот же жизненный цикл, что в tests/test_scheduler_restart_260816.py: настоящий
`init_scheduler` на временном jobstore, `asyncio.run` без pytest-asyncio.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from services import scheduler as sched
from services.scheduler import MOSCOW_TZ
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

KEYS = ("nudge_scan_minutes", "allowlist_refresh_minutes", "incomplete_sync_hours",
        "resume_retry_minutes", "chat_refresh_minutes")


def _isolate(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "intervals.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


def _stop(s):
    s.pause()
    s.shutdown(wait=False)


def test_interval_change_reschedules_without_restart(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            assert s.get_job("nudge_scan").trigger.interval == timedelta(minutes=15)
            await set_setting_by_admin(1, "nudge_scan_minutes", "45")
            job = s.get_job("nudge_scan")
            assert job.trigger.interval == timedelta(minutes=45)
            expected = datetime.now(MOSCOW_TZ) + timedelta(minutes=45)
            assert abs((job.next_run_time - expected).total_seconds()) <= 60

            # Один ключ — две джобы: «Незавершённые» и вкладка автоотказов.
            await set_setting_by_admin(1, "incomplete_sync_hours", "5")
            for job_id in ("incomplete_sheet_sync", "auto_reject_sheet_sync"):
                assert s.get_job(job_id).trigger.interval == timedelta(hours=5)

            # Сверка чата: после смены интервала первый прогон — вскоре, а не через 6+ часов.
            await set_setting_by_admin(1, "chat_refresh_minutes", "720")
            chat = s.get_job("chat_membership_refresh")
            assert chat.trigger.interval == timedelta(minutes=720)
            assert chat.next_run_time <= datetime.now(MOSCOW_TZ) + timedelta(minutes=5)
        finally:
            _stop(s)

    asyncio.run(go())


def test_same_interval_keeps_saved_schedule(tmp_path, monkeypatch):
    """Повторная запись того же значения не сдвигает уже назначенный прогон."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            s.pause()
            saved = datetime.now(MOSCOW_TZ) + timedelta(minutes=3)
            s.modify_job("allowlist_refresh", next_run_time=saved)
            await set_setting_by_admin(1, "allowlist_refresh_minutes", "60")
            after = s.get_job("allowlist_refresh").next_run_time
            assert abs((after - saved).total_seconds()) <= 1
        finally:
            _stop(s)

    asyncio.run(go())


def test_reset_to_default_and_no_scheduler_are_safe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    fast_init_db()
    # Без планировщика (процесс Mini App, тесты) — не падает и ничего не делает.
    asyncio.run(sched.on_setting_written("resume_retry_minutes"))
    asyncio.run(sched.on_setting_written("start_text"))

    async def go():
        s = await sched.init_scheduler(bot=object())
        try:
            await set_setting_by_admin(1, "resume_retry_minutes", "30")
            assert s.get_job("resume_upload_retry").trigger.interval == timedelta(minutes=30)
            from services.settings.audit import delete_setting_by_admin
            await delete_setting_by_admin(1, "resume_retry_minutes")
            assert s.get_job("resume_upload_retry").trigger.interval == timedelta(minutes=10)
        finally:
            _stop(s)

    asyncio.run(go())


def test_every_interval_key_is_live_and_says_so():
    table = sched._setting_interval_jobs()
    assert set(table) == set(KEYS)
    for key in KEYS:
        prompt = SETTINGS_SCHEMA[key]["prompt"]
        assert "перезапуск" not in prompt, key
        assert "действует сразу" in prompt, key
        for _job_id, _func, unit, default, _delay in table[key]:
            assert SETTINGS_SCHEMA[key]["default"] == default, key
            assert unit in ("minutes", "hours")
