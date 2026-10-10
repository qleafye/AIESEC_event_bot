"""Зашитые сроки → настройки реестра (бэклог «🛠», P1, ночь 10.10). Дефолт каждой настройки —
прежнее поведение; новое значение действует без перезапуска бота.

Реальный AsyncIOScheduler на временном jobstore — приём `tests/test_ambassador_wave_scheduling_32.py`.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from services import scheduler as sched
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db
from tests.test_ambassador_wave_scheduling_32 import _run_scheduled


def _ready(tmp_path, name="timings.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


# ── Напоминание о дедлайне задания: за сколько часов ─────────────────────────────────────

def test_deadline_reminder_hours_setting_default_is_old_behaviour():
    entry = SETTINGS_SCHEMA["wave_deadline_reminder_hours"]
    assert entry["type"] == "int" and entry["default"] == 24
    assert entry["min"] == 1 and entry["max"] == 168
    assert "24" in entry["prompt"]


def test_deadline_reminder_moves_when_setting_changes(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    deadline = (datetime.now() + timedelta(days=3)).replace(second=0, microsecond=0)
    task_id = _run(db.create_task("T", "Light", 10, "photo", deadline.strftime("%Y-%m-%d %H:%M:%S"), None))
    job_id = f"task_deadline_reminder_{task_id}"

    async def body(s):
        await sched.load_deadline_reminder_hours()
        await sched.reconcile_wave_jobs()
        first = s.get_job(job_id).trigger.run_date.replace(tzinfo=None)
        await db.set_setting("wave_deadline_reminder_hours", "3")
        await sched.on_setting_written("wave_deadline_reminder_hours")
        second = s.get_job(job_id).trigger.run_date.replace(tzinfo=None)
        return first, second

    first, second = _run_scheduled(tmp_path, monkeypatch, body)
    assert first == deadline - timedelta(hours=24)
    assert second == deadline - timedelta(hours=3)


def test_deadline_reminder_hours_bad_value_falls_back_to_24(tmp_path, monkeypatch):
    _ready(tmp_path, "timings_bad.db")
    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    _run(db.set_setting("wave_deadline_reminder_hours", "0"))
    assert _run(sched.load_deadline_reminder_hours()) == 24
