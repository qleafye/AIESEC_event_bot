"""Страница «Итог дня»: четыре старых среза за выбранный день, без новой арифметики.

Срез прихода не подменяет день. Опросы, оценки и резерв совпадают с уже
существующими функциями. В контексте нет ФИО и telegram id.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from config import config
from database import db
from database.db import _connect
from tests._dbtpl import fast_init_db
from tests.test_arrival_stats_260924 import _user

import shared.arrival_stats as arrival_stats
from dashboard import queries
from dashboard.day_result import build_day_context

ADMIN_ID = 581122
SEASON = "YL 26/2"
DAY = "2026-10-30"
OTHER = "2026-10-31"
ENTRY_ONLY = "2026-11-01"
PERSON = "Зинаида Приходная"
PERSON_ID = 880011


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="dashboard_day.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("event_city_enabled", "on"))
    return config.DB_PATH


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def _seed_cities():
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO cities (code, label, enabled, sort_order, created_at) VALUES "
            "('spb', 'Санкт-Петербург', 1, 1, '2026-01-01 00:00:00'), "
            "('msk', 'Москва', 1, 2, '2026-01-01 00:00:00')"
        )
        await conn.commit()


async def _seed_entry(tid, day=DAY):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (?, 'entry', ?, 'scan', ?, ?)",
            (tid, f"{day} 10:00:00", f"{day} 10:00:00", day),
        )
        await conn.commit()


async def _named(tid, name, *, city="msk", status="approved"):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, name, status, SEASON, city),
        )
        await conn.commit()


def _freeze(monkeypatch):
    from datetime import datetime as dt
    monkeypatch.setattr(queries, "msk_now", lambda: dt(2026, 10, 30, 19, 40, 0))
    monkeypatch.setattr(
        "dashboard.day_result.queries.msk_now", lambda: dt(2026, 10, 30, 19, 40, 0),
    )


def _walk_people(value):
    if isinstance(value, dict):
        assert "telegram_id" not in value
        assert "full_name" not in value
        for item in value.values():
            _walk_people(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_people(item)


def test_arrival_for_day_does_not_borrow_another_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    called = {"floor": 0}
    real_floor = queries.arrival_floor

    def _boom(*args, **kwargs):
        called["floor"] += 1
        return real_floor(*args, **kwargs)

    monkeypatch.setattr(queries, "arrival_floor", _boom)
    _run(_seed_cities())
    _run(_user(1, city="msk", season=SEASON, status="approved"))
    _run(_user(2, city="msk", season=SEASON, status="approved"))
    _run(_seed_entry(1))

    conn = _conn()
    try:
        scope = queries.Scope(city="msk", season=SEASON)
        got = queries.arrival_for_day(conn, scope, DAY)
        empty = queries.arrival_for_day(conn, scope, OTHER)
        conn.execute("DROP TABLE checkins")
        missing = queries.arrival_for_day(conn, scope, DAY)
    finally:
        conn.close()

    assert got == {"approved": 2, "present": 1, "pct": 50}
    assert empty is None
    assert missing is None
    assert called["floor"] == 0


def test_day_context_reuses_old_slices_and_hides_people(tmp_path, monkeypatch):
    _ready(tmp_path, "dashboard_day_context.db")
    _freeze(monkeypatch)
    _run(_seed_cities())
    _run(_named(PERSON_ID, PERSON))
    _run(_user(2, city="msk", season=SEASON, status="approved"))
    _run(_seed_entry(PERSON_ID))
    _run(_seed_entry(2, ENTRY_ONLY))
    _run(_seed_survey_and_session())
    _run(_seed_reserve())
    _run(db.set_setting("reject_rules_enabled", "on"))

    conn = _conn()
    try:
        scope = queries.Scope(city="msk", season=SEASON)
        context = build_day_context(
            conn, None, scope, {"telegram_id": 1, "bound_city": None}, day=DAY,
        )
        other = build_day_context(
            conn, None, scope, {"bound_city": None}, day=OTHER,
        )
        feedback = queries.feedback_day(conn, scope, day=DAY)
        reserve = queries.reserve_counts(conn, scope, DAY)
        options = context["day_options"]
    finally:
        conn.close()

    assert context["arrival"] == {"approved": 2, "present": 1, "pct": 50}
    assert other["arrival"] is None
    assert context["forms"] == feedback["forms"]
    assert context["totals"] == feedback["totals"]
    assert context["sessions"] == feedback["sessions"]
    assert context["sessions_totals"] == feedback["sessions_totals"]
    assert context["reserve"]["invited"] == reserve["invited"]
    assert context["reserve"]["arrived"] == reserve["arrived"]
    assert context["reserve"]["invited"] == 1
    assert context["reserve"]["arrived"] == 1
    assert context["reserve_off"] is False
    labels = {row["day"]: row["label"] for row in options}
    assert labels[DAY] == "30.10"
    assert labels[OTHER] == "31.10"
    assert labels[ENTRY_ONLY] == arrival_stats.day_short(ENTRY_ONLY)
    assert [row["day"] for row in options] == [DAY, OTHER, ENTRY_ONLY]
    blob = json.dumps(context, ensure_ascii=False, default=str)
    assert PERSON not in blob
    assert str(PERSON_ID) not in blob
    _walk_people(context)


def test_reserve_off_is_a_mark_not_zeros(tmp_path, monkeypatch):
    _ready(tmp_path, "dashboard_day_reserve_off.db")
    _freeze(monkeypatch)
    _run(_seed_cities())
    _run(_named(PERSON_ID, PERSON, status="rejected"))
    _run(_seed_reserve())

    conn = _conn()
    try:
        context = build_day_context(
            conn, None, queries.Scope(city="msk", season=SEASON),
            {"bound_city": None}, day=DAY,
        )
    finally:
        conn.close()

    assert context["reserve"] is None
    assert context["reserve_off"] is True


def test_reserve_zero_stays_a_number(tmp_path, monkeypatch):
    _ready(tmp_path, "dashboard_day_reserve_zero.db")
    _freeze(monkeypatch)
    _run(db.set_setting("reject_rules_enabled", "on"))
    conn = _conn()
    try:
        context = build_day_context(
            conn, None, queries.Scope(season=SEASON), {"bound_city": None}, day=DAY,
        )
    finally:
        conn.close()

    assert context["reserve"]["invited"] == 0
    assert context["reserve"]["arrived"] == 0


async def _seed_survey_and_session():
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, "
            "created_at, city, audience_json, status, scheduled_at, sending_since, kind) "
            "VALUES ('Как старт', '[]', 0, 0, ?, 'msk', '[]', 'open', ?, ?, 'survey')",
            (f"{DAY} 09:00:00", f"{DAY} 12:30:00", f"{DAY} 12:30:00"),
        )
        await conn.execute(
            "INSERT INTO program_sessions "
            "(city, day, start_time, end_time, title, created_at, updated_at) "
            "VALUES ('msk', ?, '13:30', '14:00', 'Рынок меняется', ?, ?), "
            "('msk', ?, '15:00', '16:00', 'Только программа', ?, ?)",
            (DAY, f"{DAY} 09:00:00", f"{DAY} 09:00:00",
             OTHER, f"{OTHER} 09:00:00", f"{OTHER} 09:00:00"),
        )
        await conn.commit()


async def _seed_reserve():
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city) "
            "VALUES (7, 'Резерв Семь', 'rejected', ?, 'msk')",
            (SEASON,),
        )
        await conn.execute(
            "INSERT INTO auto_reject_log (telegram_id, rule_ids, reject_texts, "
            "attempt_count, first_triggered_at, last_triggered_at) "
            "VALUES (7, '[1]', '[]', 1, ?, ?)",
            (f"{DAY} 09:00:00", f"{DAY} 09:00:00"),
        )
        await conn.execute(
            "INSERT INTO reserve_invites (telegram_id, day, city, invited_by, invited_at) "
            "VALUES (7, ?, 'msk', 1, ?)",
            (DAY, f"{DAY} 09:00:00"),
        )
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (7, 'entry', ?, 'scan', ?, ?)",
            (f"{DAY} 11:00:00", f"{DAY} 11:00:00", DAY),
        )
        await conn.commit()
