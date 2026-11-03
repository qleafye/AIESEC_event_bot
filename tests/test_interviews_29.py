"""Запись на интервью: окно, кап, один перенос, напоминание за 2 часа.

Экранов нет. Пока тумблер выключен, запись не пишет строку и напоминание молчит.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from database import db, interview_db
from domain.settings.schema import SETTINGS_SCHEMA
from services import interviews, scheduler
from tests._dbtpl import fast_init_db
from tests._enroll38 import add_user, ready, run

_NOW = datetime(2026, 11, 3, 12, 0)
_FORBIDDEN = ("отбор", "не прошёл", "не прошел", "отказ")
_TEXT_KEYS = (
    "interview_reminder_text", "interview_booked_text", "interview_moved_text",
    "interview_full_text", "interview_closed_text", "interview_reschedule_spent_text",
)


class _Jobs:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger, run_date=None, args=None, id=None, replace_existing=False):
        self.jobs[id] = {"fn": fn, "run": run_date, "args": list(args or [])}

    def remove_job(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        del self.jobs[job_id]


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def _open(tmp_path):
    ready(tmp_path)
    run(db.set_setting("interview_booking_enabled", "on"))
    run(db.set_setting("interview_window_start", "01.01.2020 00:00"))
    run(db.set_setting("interview_window_end", "01.01.2100 00:00"))


async def _person(tid, status="approved", case="Да"):
    await add_user(tid, status=status, season=None)
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET case_optin = ? WHERE telegram_id = ?", (case, tid),
        )
        await conn.commit()
    return await db.get_user(tid)


def _slot(starts="2099-01-01 16:00", cap=1):
    return run(interview_db.create_slot(None, starts, cap, None, "2026-11-03 12:00:00"))


def test_tables_created_without_checked_in(tmp_path):
    ready(tmp_path)

    async def look():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'",
            ) as cur:
                rows = await cur.fetchall()
        return rows

    rows = run(look())
    names = {name for name, _sql in rows}
    blob = "\n".join(sql or "" for _name, sql in rows)
    assert "interview_slots" in names
    assert "interview_bookings" in names
    assert "checked_in_at" not in blob


def test_text_defaults_have_no_forbidden_words():
    for key in _TEXT_KEYS:
        default = SETTINGS_SCHEMA[key]["default"].lower()
        for word in _FORBIDDEN:
            assert word not in default, key
        assert SETTINGS_SCHEMA[key]["group"] == "reg"
    assert SETTINGS_SCHEMA["interview_booking_enabled"]["default"] == "off"
    assert SETTINGS_SCHEMA["interview_slot_cap"]["default"] == 1


def test_book_requires_yes_label(tmp_path):
    _open(tmp_path)
    yes = run(_person(1, case="Да"))
    no = run(_person(2, case="Нет"))
    empty = run(_person(3, case=""))
    digit = run(_person(4, case="1"))
    slot = _slot(cap=4)
    assert run(interviews.book_slot(yes, slot, now=_NOW)) is None
    for user in (no, empty, digit):
        err = run(interviews.book_slot(user, slot, now=_NOW))
        assert err
        low = err.lower()
        for word in _FORBIDDEN:
            assert word not in low
    assert run(interview_db.slot_taken(slot)) == 1


def test_book_rejects_rejected_status(tmp_path):
    _open(tmp_path)
    slot = _slot(cap=3)
    rejected = run(_person(1, status="rejected"))
    assert run(interviews.book_slot(rejected, slot, now=_NOW))
    for tid, status in ((2, "pending"), (3, "approved"), (4, "waitlist")):
        user = run(_person(tid, status=status))
        assert run(interviews.book_slot(user, slot, now=_NOW)) is None
    assert run(interview_db.slot_taken(slot)) == 3


def test_window_closed(tmp_path):
    ready(tmp_path)
    run(db.set_setting("interview_booking_enabled", "on"))
    user = run(_person(1))
    slot = _slot()
    run(db.set_setting("interview_window_start", "01.01.2099 00:00"))
    run(db.set_setting("interview_window_end", "02.01.2099 00:00"))
    early = run(interviews.book_slot(user, slot, now=_NOW))
    assert "ещё не открыта" in early.lower()
    run(db.set_setting("interview_window_start", "01.01.2020 00:00"))
    run(db.set_setting("interview_window_end", "02.01.2020 00:00"))
    late = run(interviews.book_slot(user, slot, now=_NOW))
    assert "запись закрыта" in late.lower()
    assert run(interview_db.get_booking(1)) is None
    run(db.set_setting("interview_window_start", ""))
    run(db.set_setting("interview_window_end", "01.01.2100 00:00"))
    assert run(interviews.book_slot(user, slot, now=_NOW))


def test_cap_race(tmp_path):
    _open(tmp_path)
    slot = _slot(cap=1)
    first = run(_person(1))
    second = run(_person(2))
    assert run(interviews.book_slot(first, slot, now=_NOW)) is None
    err = run(interviews.book_slot(second, slot, now=_NOW))
    assert "это время уже занято" in err.lower()
    assert run(interview_db.get_booking(2)) is None
    assert run(interview_db.slot_taken(slot)) == 1


def test_reschedule_once(tmp_path):
    _open(tmp_path)
    first = _slot(starts="2099-01-01 16:00", cap=1)
    second = _slot(starts="2099-01-01 18:00", cap=1)
    third = _slot(starts="2099-01-02 10:00", cap=1)
    user = run(_person(1))
    assert run(interviews.book_slot(user, first, now=_NOW)) is None
    assert run(interviews.move_slot(user, second, now=_NOW)) is None
    booking = run(interview_db.get_booking(1))
    assert booking["slot_id"] == second
    assert booking["reschedule_count"] == 1
    err = run(interviews.move_slot(user, third, now=_NOW))
    assert err
    assert "один раз" in err.lower()
    assert run(interview_db.get_booking(1))["slot_id"] == second
    same = run(interviews.move_slot(user, second, now=_NOW))
    assert "это и есть твоё время" in same.lower()


def test_toggle_off_refuses(tmp_path):
    ready(tmp_path)
    run(db.set_setting("interview_window_start", "01.01.2020 00:00"))
    run(db.set_setting("interview_window_end", "01.01.2100 00:00"))
    user = run(_person(1))
    slot = _slot()
    assert run(interviews.book_slot(user, slot, now=_NOW))
    assert run(interview_db.get_booking(1)) is None


def _arm(monkeypatch):
    jobs = _Jobs()
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: jobs)
    return jobs


def test_reminder_scheduled_two_hours_before(tmp_path, monkeypatch):
    _open(tmp_path)
    jobs = _arm(monkeypatch)
    user = run(_person(7))
    first = _slot(starts="2099-01-01 16:00")
    second = _slot(starts="2099-01-01 18:00")
    assert run(interviews.book_slot(user, first, now=_NOW)) is None
    job_id = "interview_rem_7"
    assert list(jobs.jobs) == [job_id]
    assert jobs.jobs[job_id]["run"] == datetime(2099, 1, 1, 14, 0)
    assert jobs.jobs[job_id]["args"] == [7, first]
    assert run(interviews.move_slot(user, second, now=_NOW)) is None
    assert list(jobs.jobs) == [job_id]
    assert jobs.jobs[job_id]["run"] == datetime(2099, 1, 1, 16, 0)
    assert jobs.jobs[job_id]["args"] == [7, second]


def test_reminder_noop_if_moved(tmp_path, monkeypatch):
    _open(tmp_path)
    bot = _Bot()
    scheduler._bot = bot
    user = run(_person(8))
    slot = _slot()
    other = _slot(starts="2099-01-01 18:00")
    assert run(interviews.book_slot(user, slot, now=_NOW)) is None
    run(scheduler.send_interview_reminder(8, other))
    assert bot.sent == []


def test_reminder_noop_if_cancelled(tmp_path):
    _open(tmp_path)
    bot = _Bot()
    scheduler._bot = bot
    user = run(_person(9))
    slot = _slot()
    assert run(interviews.book_slot(user, slot, now=_NOW)) is None
    run(interview_db.delete_slot(slot))
    run(scheduler.send_interview_reminder(9, slot))
    assert bot.sent == []


def test_reminder_noop_if_toggle_off(tmp_path):
    _open(tmp_path)
    bot = _Bot()
    scheduler._bot = bot
    user = run(_person(10))
    slot = _slot()
    assert run(interviews.book_slot(user, slot, now=_NOW)) is None
    run(db.set_setting("interview_booking_enabled", "off"))
    run(scheduler.send_interview_reminder(10, slot))
    assert bot.sent == []


def test_reminder_sends_registry_text(tmp_path):
    _open(tmp_path)
    bot = _Bot()
    scheduler._bot = bot
    user = run(_person(11))
    slot = _slot(starts="2099-01-01 16:00")
    assert run(interviews.book_slot(user, slot, now=_NOW)) is None
    run(scheduler.send_interview_reminder(11, slot))
    assert bot.sent == [(11, "Напоминание: интервью 01.01.2099 16:00. До встречи.")]


def test_quiet_hours_do_not_push_past_slot(tmp_path, monkeypatch):
    _open(tmp_path)
    jobs = _arm(monkeypatch)
    bot = _Bot()
    scheduler._bot = bot
    user = run(_person(12))
    slot = _slot(starts="2099-01-01 16:00")
    assert run(interviews.book_slot(user, slot, now=_NOW)) is None
    starts = datetime(2099, 1, 1, 16, 0)

    async def too_late(now, user_id):
        return starts

    monkeypatch.setattr("services.comms.quiet_hours.defer_until", too_late)
    jobs.jobs.clear()
    run(scheduler.send_interview_reminder(12, slot))
    assert bot.sent == [(12, "Напоминание: интервью 01.01.2099 16:00. До встречи.")]
    assert jobs.jobs == {}

    bot.sent.clear()

    async def still_before(now, user_id):
        return starts - timedelta(minutes=30)

    monkeypatch.setattr("services.comms.quiet_hours.defer_until", still_before)
    run(scheduler.send_interview_reminder(12, slot))
    assert bot.sent == []
    assert jobs.jobs["interview_rem_12"]["run"] == starts - timedelta(minutes=30)


def test_no_numeric_case_optin_comparison():
    text = open("services/interviews.py", encoding="utf-8").read()
    assert '== 1' not in text
    assert '== "1"' not in text
    assert "checked_in_at" not in text
