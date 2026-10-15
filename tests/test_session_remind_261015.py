"""Напоминания о сессиях: за N минут до начала (записанным) и утренняя «программа на день».

pytest-asyncio нет — корутины через `asyncio.run`, БД — шаблонная копия `fast_init_db`
(`tests/_enroll38.py`), планирование — реальный `AsyncIOScheduler` на временном jobstore (приём
`tests/test_session_feedback_260924.py`). «Сейчас» подменяется у `services.scheduler`."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import services.scheduler as sched
import services.forum.session_remind as sr
from database import db, session_enroll_db as edb, session_remind_db as rdb
from handlers.forum import admin_session_remind as ui
from handlers.states import EditSetting
from domain.settings.validation import validate_setting_value
from tests._enroll38 import ADMIN_ID, CITY, DAY, add_user, ready, run, seed_msk_program
from tests.test_admin_enroll_38 import FakeCallback, cbs, new_state

# Сессия A — 10:00 в зале «Зал 1»; пленарка P — 09:00 без трека (общая).
NOW_MORNING = datetime(2026, 10, 30, 7, 0)
NOW_09_45 = datetime(2026, 10, 30, 9, 50)  # момент напоминания (10:00 − 10 мин)
NOW_10_05 = datetime(2026, 10, 30, 10, 5)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(sr, "_THROTTLE_SECONDS", 0)


def _setup(monkeypatch, now):
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: now)
    return bot


async def _on(*keys):
    for key in keys:
        await db.set_setting(key, "on")


def _build_scheduler(tmp_path):
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.sqlite'}")},
        timezone=sched.MOSCOW_TZ,
    )


def _run_scheduled(tmp_path, monkeypatch, body):
    s = _build_scheduler(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", s)

    async def go():
        s.start(paused=True)
        try:
            return await body(s)
        finally:
            s.shutdown(wait=False)

    return asyncio.run(go())


# ── Чистые функции ───────────────────────────────────────────────────────────────────────────

def test_remind_run_at_uses_city_offset():
    session = {"day": "2026-10-30", "start_time": "10:00"}
    assert sr.remind_run_at(session, 10, 0) == datetime(2026, 10, 30, 9, 50)
    # Тюмень МСК+2: 10:00 местного = 08:00 МСК, за 10 минут — 07:50 МСК.
    assert sr.remind_run_at(session, 10, 2) == datetime(2026, 10, 30, 7, 50)
    assert sr.remind_run_at({"day": "x", "start_time": "y"}, 10, 0) is None


def test_morning_run_at_uses_city_offset_and_falls_back_on_garbage():
    assert sr.morning_run_at("2026-10-30", "08:30", 0) == datetime(2026, 10, 30, 8, 30)
    assert sr.morning_run_at("2026-10-30", "08:30", 2) == datetime(2026, 10, 30, 6, 30)
    assert sr.morning_run_at("2026-10-30", "ерунда", 0) == datetime(2026, 10, 30, 8, 30)


def test_parse_hhmm_and_clamp():
    assert sr.parse_hhmm("8:05") == (8, 5)
    assert sr.parse_hhmm("24:00") is None
    assert sr.parse_hhmm("абв") is None
    assert sr.clamp_minutes("15") == 15
    assert sr.clamp_minutes("0") == 10 and sr.clamp_minutes("") == 10


# ── Получатели ───────────────────────────────────────────────────────────────────────────────

def test_remind_recipients_enrolled_approved_same_city_only(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        await add_user(101)                       # записан, одобрен
        await add_user(102)                       # записался и отписался
        await add_user(103, status="pending")     # не одобрен
        await add_user(104)                       # не записан
        await add_user(105, city="spb")           # другой город
        for tid in (101, 102, 103, 105):
            await edb.enroll_tx(tid, ids["A"])
        await edb.unenroll(102, ids["A"])
        session = await db.get_program_session(ids["A"])
        return [u["telegram_id"] for u in await sr.remind_recipients(session)]

    assert run(go()) == [101]


# ── Тексты ───────────────────────────────────────────────────────────────────────────────────

def test_remind_text_with_hall_and_competency(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        cid = await edb.create_competency(CITY, "Лидерство")
        await edb.toggle_session_competency(ids["A"], cid)
        session = await db.get_program_session(ids["A"])
        return await sr.build_remind_text(session, 10)

    text = run(go())
    assert "Через 10 мин" in text and "«Сессия A»" in text
    assert "10:00" in text and "Зал «Зал 1»" in text
    assert "компетенцию: Лидерство" in text


def test_remind_text_without_competency_drops_the_line(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        return await sr.build_remind_text(await db.get_program_session(ids["A"]), 15)

    text = run(go())
    assert "Через 15 мин" in text
    assert "компетенци" not in text
    assert "«»" not in text and "{" not in text


def test_remind_text_without_hall_drops_the_hall_line(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_msk_program()
        sid = await db.create_program_session(CITY, DAY, "14:00", "15:00", "Без зала")
        return await sr.build_remind_text(await db.get_program_session(sid), 10)

    text = run(go())
    assert "Зал" not in text and "«»" not in text
    assert "«Без зала»" in text and "14:00" in text


def test_remind_text_escapes_html_in_title(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_msk_program()
        sid = await db.create_program_session(CITY, DAY, "14:00", "15:00", "A <b> & B")
        return await sr.build_remind_text(await db.get_program_session(sid), 10)

    assert "A &lt;b&gt; &amp; B" in run(go())


# ── Отправка напоминаний ─────────────────────────────────────────────────────────────────────

def test_deliver_reminders_once_and_idempotent(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_09_45)

    async def go():
        ids = await seed_msk_program()
        await _on("session_remind_enabled")
        await add_user(101)
        await add_user(102)
        await edb.enroll_tx(101, ids["A"])
        first = await sr.deliver_session_reminders(ids["A"])
        second = await sr.deliver_session_reminders(ids["A"])
        return first, second

    first, second = run(go())
    assert first["sent"] == 1 and second["sent"] == 0
    assert [chat for chat, _ in bot.sent] == [101]
    assert "Сессия A" in bot.sent[0][1]


def test_deliver_reminders_unenrolled_between_schedule_and_fire_gets_nothing(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_09_45)

    async def go():
        ids = await seed_msk_program()
        await _on("session_remind_enabled")
        await add_user(101)
        await edb.enroll_tx(101, ids["A"])
        await edb.unenroll(101, ids["A"])
        return await sr.deliver_session_reminders(ids["A"])

    assert run(go())["sent"] == 0
    assert bot.sent == []


def test_deliver_reminders_switched_off_sends_nothing(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_09_45)

    async def go():
        ids = await seed_msk_program()
        await add_user(101)
        await edb.enroll_tx(101, ids["A"])
        return await sr.deliver_session_reminders(ids["A"])  # тумблер по умолчанию выключен

    assert run(go())["sent"] == 0
    assert bot.sent == []


def test_deliver_reminders_not_after_session_start(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_10_05)

    async def go():
        ids = await seed_msk_program()
        await _on("session_remind_enabled")
        await add_user(101)
        await edb.enroll_tx(101, ids["A"])
        return await sr.deliver_session_reminders(ids["A"])

    assert run(go())["sent"] == 0
    assert bot.sent == []


def test_deliver_reminders_deleted_session_is_silent(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_09_45)

    async def go():
        ids = await seed_msk_program()
        await _on("session_remind_enabled")
        await add_user(101)
        await edb.enroll_tx(101, ids["A"])
        await db.delete_program_session(ids["A"])
        return await sr.deliver_session_reminders(ids["A"])

    assert run(go())["sent"] == 0
    assert bot.sent == []


def test_past_season_delegate_gets_no_reminder(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_09_45)

    async def go():
        from tests._enroll38 import PAST_SEASON, SEASON

        ids = await seed_msk_program()
        await db.set_setting("event_season", SEASON)
        await _on("session_remind_enabled")
        await add_user(101, season=PAST_SEASON)
        await edb.enroll_tx(101, ids["A"])
        return await sr.deliver_session_reminders(ids["A"])

    assert run(go())["sent"] == 0
    assert bot.sent == []


# ── Утренняя программа ───────────────────────────────────────────────────────────────────────

def test_morning_program_has_chosen_and_common_for_that_day_only(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_MORNING)

    async def go():
        ids = await seed_msk_program()
        cid = await edb.create_competency(CITY, "Лидерство")
        await edb.toggle_session_competency(ids["A"], cid)
        # Сессия другого дня — в программу «сегодня» попасть не должна.
        other = await db.create_program_session(CITY, "2026-10-31", "10:00", "11:00", "Завтрашняя", hall_id=ids["hall1"])
        await db.update_program_session(other, track_id=ids["career"])
        await _on("session_morning_enabled")
        await add_user(101)
        await edb.enroll_tx(101, ids["A"])
        await edb.enroll_tx(101, other)
        return await sr.deliver_morning_program(CITY, DAY)

    result = run(go())
    assert result["sent"] == 1
    (chat, text), = bot.sent
    assert chat == 101
    assert text.startswith("☀️ Доброе утро! Твоя программа на сегодня:")
    assert "«Сессия A»" in text and "зал Зал 1" in text and "🎯 Лидерство" in text
    assert "«Пленарка»" in text and "(для всех)" in text
    assert "Завтрашняя" not in text and "Сессия B" not in text
    assert text.index("Пленарка") < text.index("Сессия A")  # по времени


def test_morning_program_idempotent_and_skips_pending_and_off_switch(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_MORNING)

    async def go():
        await seed_msk_program()
        await add_user(101)
        await add_user(102, status="pending")
        off = await sr.deliver_morning_program(CITY, DAY)  # тумблер выключен
        await _on("session_morning_enabled")
        first = await sr.deliver_morning_program(CITY, DAY)
        second = await sr.deliver_morning_program(CITY, DAY)
        return off, first, second

    off, first, second = run(go())
    assert off["sent"] == 0
    assert first["sent"] == 1 and second["sent"] == 0
    assert [chat for chat, _ in bot.sent] == [101]  # у делегата только общая пленарка


def test_morning_program_not_sent_when_day_already_started(tmp_path, monkeypatch):
    ready(tmp_path)
    bot = _setup(monkeypatch, datetime(2026, 10, 30, 9, 30))  # пленарка в 09:00 уже идёт

    async def go():
        await seed_msk_program()
        await _on("session_morning_enabled")
        await add_user(101)
        return await sr.deliver_morning_program(CITY, DAY)

    assert run(go())["sent"] == 0
    assert bot.sent == []


def test_blocked_delegate_does_not_break_the_run(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    ready(tmp_path)
    bot = _setup(monkeypatch, NOW_MORNING)

    async def send_message(chat_id, text, **kw):
        if chat_id == 101:
            raise TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked")
        bot.sent.append((chat_id, text))

    bot.send_message = send_message

    async def go():
        await seed_msk_program()
        await _on("session_morning_enabled")
        await add_user(101)
        await add_user(102)
        result = await sr.deliver_morning_program(CITY, DAY)
        return result, await rdb.was_sent(101, rdb.KIND_MORNING, f"{CITY}:{DAY}")

    result, reserved = run(go())
    assert result["sent"] == 1
    assert [chat for chat, _ in bot.sent] == [102]
    assert reserved  # заблокировавший остаётся в журнале: повторять нечего


# ── Планирование ─────────────────────────────────────────────────────────────────────────────

def test_switched_off_places_no_jobs(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def body(s):
        await seed_msk_program()
        await sr.reconcile()
        return [j.id for j in s.get_jobs()]

    assert _run_scheduled(tmp_path, monkeypatch, body) == []


def test_switched_on_places_reminder_and_morning_jobs_at_local_to_msk_time(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def body(s):
        ids = await seed_msk_program()
        await _on("session_remind_enabled", "session_morning_enabled")
        await sr.reconcile()
        jobs = {j.id: j for j in s.get_jobs()}
        return ids, jobs

    ids, jobs = _run_scheduled(tmp_path, monkeypatch, body)
    remind = jobs[sr.remind_job_id(ids["A"])]
    assert remind.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 30, 9, 50)
    morning = jobs[sr.morning_job_id(CITY, DAY)]
    assert morning.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 30, 8, 30)
    assert len([j for j in jobs if j.startswith("session_remind:")]) == 5  # по джобе на сессию


def test_reconcile_is_idempotent_and_cancel_removes_deleted_session_job(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def body(s):
        ids = await seed_msk_program()
        await _on("session_remind_enabled")
        await sr.reconcile()
        await sr.reconcile()
        count = len(s.get_jobs())
        await db.delete_program_session(ids["A"])
        await sr.on_session_changed(ids["A"], CITY)
        return count, [j.id for j in s.get_jobs()], ids

    count, left, ids = _run_scheduled(tmp_path, monkeypatch, body)
    assert count == 5
    assert sr.remind_job_id(ids["A"]) not in left and len(left) == 4


def test_missed_morning_slot_is_caught_up_before_first_session_only(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, datetime(2026, 10, 30, 8, 45))  # 08:30 уже прошло, пленарка в 09:00 ещё нет

    async def body(s):
        await seed_msk_program()
        await _on("session_morning_enabled")
        await sr.reconcile()
        return {j.id: j for j in s.get_jobs()}

    jobs = _run_scheduled(tmp_path, monkeypatch, body)
    assert sr.morning_job_id(CITY, DAY) in jobs

    ready(tmp_path, "late.db")
    _setup(monkeypatch, datetime(2026, 10, 30, 9, 30))  # первая сессия дня уже началась

    async def body_late(s):
        await seed_msk_program()
        await _on("session_morning_enabled")
        await sr.reconcile()
        return [j.id for j in s.get_jobs()]

    assert _run_scheduled(tmp_path, monkeypatch, body_late) == []


def test_settings_write_reschedules_via_hook(tmp_path, monkeypatch):
    """Смена настроек напоминаний идёт через общий хук перепланировки (`reschedule._plan`)."""
    from services.settings.reschedule import _plan

    assert _plan(["session_remind_minutes__city__msk", "city_tz_offset__city__spb"]) == [
        ("session_remind", "msk", False), ("session_remind", "spb", False),
    ]
    assert _plan(["session_morning_time"]) == [("session_remind", None, True)]  # общий ключ -> все города


# ── Экран менеджера ──────────────────────────────────────────────────────────────────────────

def test_screen_shows_status_and_all_buttons(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def body(s):
        await seed_msk_program()
        await _on("session_remind_enabled")
        await sr.reconcile()
        return await ui.render_remind_screen(CITY)

    text, kb = _run_scheduled(tmp_path, monkeypatch, body)
    assert "Запланировано на 30.10: 5 напоминаний из 5 сессий" in text
    assert "утренняя программа не запланирована" in text
    data = cbs(kb)
    for prefix in ("prog_srmon", "prog_srmmo", "prog_srmtx", "prog_srmtm", "prog_srmex", "prog_enrset"):
        assert any(c.startswith(prefix) for c in data), prefix
    assert [c for c in data if c.startswith("prog_srmmin")] == [f"prog_srmmin:msk:{m}" for m in (5, 10, 15, 30)]
    assert not any("session_remind" in t for t in [b.text for row in kb.inline_keyboard for b in row])


def test_toggle_and_minutes_handlers_save_and_rerender(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def go():
        await seed_msk_program()
        cb = FakeCallback("prog_srmon:msk")
        await ui.prog_srmon(cb)
        on = await sr.remind_enabled(CITY)
        cb2 = FakeCallback("prog_srmmin:msk:15")
        await ui.prog_srmmin(cb2)
        mo = FakeCallback("prog_srmmo:msk")
        await ui.prog_srmmo(mo)
        bad = FakeCallback("prog_srmmin:msk:7")
        await ui.prog_srmmin(bad)
        return on, await sr.remind_minutes(CITY), await sr.morning_enabled(CITY), cb, bad

    on, minutes, morning, cb, bad = run(go())
    assert on is True and minutes == 15 and morning is True
    assert "Напоминать за 15 мин" in cb.message.text_edited or cb.message.text_edited
    assert bad.answers and bad.answers[0][1] is True and "5, 10, 15 или 30" in bad.answers[0][0]


def test_text_and_time_editors_open_generic_edit_with_return_to_screen(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_msk_program()
        state = new_state()
        cb = FakeCallback("prog_srmtm:msk")
        await ui.prog_srmtm(cb, state)
        time_state = (await state.get_state(), await state.get_data())
        cb2 = FakeCallback("prog_srmtx:msk:remind")
        await ui.prog_srmtx(cb2, state)
        return time_state, await state.get_data(), cb2

    (st, data), text_data, cb2 = run(go())
    assert st == EditSetting.waiting_for_value.state
    assert data["setting_key"] == "session_morning_time" and data["return_cb"] == "prog_srm:msk"
    assert text_data["setting_key"] == "session_remind_text"
    assert "{minutes}" in cb2.message.text_edited and "Подстановки" in cb2.message.text_edited


def test_invalid_time_gives_human_error_and_valid_time_is_normalised():
    value, error = validate_setting_value("session_morning_time", "25:99")
    assert value is None and "ЧЧ:ММ" in error or "от 0 до 23" in error
    value, error = validate_setting_value("session_morning_time", "утро")
    assert value is None and "ЧЧ:ММ" in error
    assert validate_setting_value("session_morning_time", "7:05") == ("07:05", None)


def test_example_sends_remind_and_morning_and_explains_empty_program(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def go():
        empty = FakeCallback("prog_srmex:msk")
        await ui.prog_srmex(empty)
        await seed_msk_program()
        full = FakeCallback("prog_srmex:msk")
        await ui.prog_srmex(full)
        return empty, full

    empty, full = run(go())
    assert empty.answers[0][1] is True and "нет сессий" in empty.answers[0][0]
    sent = full.message.answers_sent
    assert len(sent) == 4 and "Через 10 мин" in sent[1] and "Доброе утро" in sent[3]


def test_enroll_settings_screen_links_to_reminders(tmp_path):
    ready(tmp_path)
    from handlers.forum import admin_enroll_list as el

    _text, kb = run(el.render_enroll_settings(CITY))
    assert "prog_srm:msk" in cbs(kb)


# ── Копирование дня и предупреждение о времени утра ──────────────────────────────────────────

def test_copy_day_schedules_remind_jobs_for_new_sessions(tmp_path, monkeypatch):
    from services.forum.program import copy_program_day

    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def body(s):
        await seed_msk_program()
        await _on("session_remind_enabled", "session_morning_enabled")
        await db.set_setting("event_city_enabled", "off")
        # Копия в другой город на тот же день: джобы должны встать сразу, без рестарта.
        result = await copy_program_day(CITY, "spb", DAY)
        return result, [j.id for j in s.get_jobs()]

    result, jobs = _run_scheduled(tmp_path, monkeypatch, body)
    assert result["sessions_created"] == 5
    assert len([j for j in jobs if j.startswith("session_remind:")]) == 5
    assert sr.morning_job_id("spb", DAY) in jobs


def test_morning_time_warning_when_not_earlier_than_first_session(tmp_path, monkeypatch):
    ready(tmp_path)
    _setup(monkeypatch, NOW_MORNING)

    async def go():
        await seed_msk_program()  # первая сессия дня — пленарка 09:00
        none_when_off = await sr.morning_time_warning(CITY)
        await _on("session_morning_enabled")
        fine = await sr.morning_time_warning(CITY)  # 08:30 < 09:00
        await db.set_setting("session_morning_time", "09:15")
        bad = await sr.morning_time_warning(CITY)
        screen, _kb = await ui.render_remind_screen(CITY)
        return none_when_off, fine, bad, screen

    off, fine, bad, screen = run(go())
    assert off is None and fine is None
    assert bad == "⚠️ 30.10 первая сессия в 09:00 — программа в 09:15 уже не уйдёт. Поставь время раньше."
    assert bad in screen
