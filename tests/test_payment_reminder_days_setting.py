"""Сроки напоминаний об оплате берутся из настроек; id джоб minus3d/minus1d неизменны."""
import asyncio
from datetime import datetime

from config import config
from database.db import add_user, set_setting

import services.scheduler as sched_mod
import handlers.payment as pay
from tests._dbtpl import fast_init_db

TELEGRAM_ID = 261019001


def _run(tmp_path, settings):
    config.DB_PATH = str(tmp_path / "test_payment_reminder_days.db")
    fast_init_db()
    asyncio.run(add_user({"telegram_id": TELEGRAM_ID, "full_name": "R", "registration_date": "x"}))
    asyncio.run(set_setting("payment_deadline", "20.07.2026 15:00"))
    for k, v in settings.items():
        asyncio.run(set_setting(k, v))
    calls = []
    orig_now, orig_sched = sched_mod._now_moscow_naive, sched_mod.schedule_payment_reminder
    sched_mod._now_moscow_naive = lambda: datetime(2026, 6, 1, 10, 0)
    sched_mod.schedule_payment_reminder = lambda uid, run_at, label: calls.append((run_at, label))
    try:
        asyncio.run(pay._schedule_deadline_reminders(TELEGRAM_ID))
    finally:
        sched_mod._now_moscow_naive, sched_mod.schedule_payment_reminder = orig_now, orig_sched
    return calls


def test_defaults_are_3_and_1(tmp_path):
    assert _run(tmp_path, {}) == [
        (datetime(2026, 7, 17, 15, 0), "minus3d"),
        (datetime(2026, 7, 19, 15, 0), "minus1d"),
    ]


def test_first_reminder_five_days_keeps_job_id(tmp_path):
    calls = _run(tmp_path, {"payment_reminder_first_days": "5"})
    assert (datetime(2026, 7, 15, 15, 0), "minus3d") in calls
    assert (datetime(2026, 7, 19, 15, 0), "minus1d") in calls


def test_zero_disables_first_reminder(tmp_path):
    calls = _run(tmp_path, {"payment_reminder_first_days": "0"})
    assert [label for _, label in calls] == ["minus1d"]


def test_zero_disables_second_reminder(tmp_path):
    calls = _run(tmp_path, {"payment_reminder_second_days": "0"})
    assert [label for _, label in calls] == ["minus3d"]
