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


def test_rejected_entry_day_is_a_button_and_query_sticks(tmp_path, monkeypatch):
    """Вход резерва (status=rejected) даёт кнопку дня. Счётчик прихода его не считает."""
    from tests import test_dashboard_render as tdr

    reserve_day = "2026-11-26"
    path = _ready(tmp_path, "dashboard_day_reserve_entry.db")
    _freeze(monkeypatch)
    _run(_seed_cities())
    _run(_program(DAY, "Первый"))
    _run(_program(OTHER, "Второй"))
    _run(_named(7, "Резерв Семь", city="msk", status="rejected"))
    _run(_seed_entry(7, reserve_day))

    conn = _conn()
    try:
        scope = queries.Scope(city="msk", season=SEASON)
        options = [row["day"] for row in build_day_context(
            conn, None, scope, {"bound_city": None}, day=reserve_day,
        )["day_options"]]
        context = build_day_context(
            conn, None, scope, {"bound_city": None}, day=reserve_day,
        )
    finally:
        conn.close()

    assert reserve_day in options
    assert context["day"] == reserve_day
    assert context["arrival"] is None

    client = tdr._stats_manager_client(path)
    page = client.get("/day", params={"city": "msk", "season": SEASON, "day": reserve_day})
    assert page.status_code == 200
    assert "26.11" in page.text
    assert "Отметок на входе ещё нет." in page.text


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


def test_day_page_shows_one_day_and_keeps_forum_totals(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    monkeypatch.setattr("dashboard.main.notify_access_request", lambda *a, **k: None)
    path = _ready(tmp_path, "dashboard_day_http.db")
    _run(_seed_cities())
    _run(_named(11, PERSON))
    for tid in (12, 13, 14):
        _run(_named(tid, f"Номер {tid}"))
    for tid in (11, 12, 13, 14):
        _run(_seed_entry(tid))
    _run(_program(OTHER, "Только программа"))
    _run(db.set_setting("reject_rules_enabled", "on"))

    guest = tdr._client(tdr._cfg(path))
    denied_guest = guest.get("/day", follow_redirects=False)
    assert denied_guest.status_code in (302, 307)
    assert denied_guest.headers["location"] == "/login"

    outsider = tdr._client(tdr._cfg(path))
    tdr._login_as(outsider, 900500)
    denied = outsider.get("/day")
    assert denied.status_code == 403
    assert "Нет доступа" in denied.text

    client = tdr._stats_manager_client(path)
    page = client.get("/day", params={"city": "msk", "season": SEASON, "day": DAY})
    assert page.status_code == 200
    text = page.text
    assert "<h1>Итог дня</h1>" in text
    for title in ("Приход", "Опросы", "Оценки сессий", "Резерв"):
        assert title in text
    assert ">4</div>" in text
    assert "Отметок на входе ещё нет." not in text
    assert PERSON not in text
    assert "Номер 12" not in text

    empty = client.get("/day", params={"city": "msk", "season": SEASON, "day": OTHER})
    assert "Отметок на входе ещё нет." in empty.text
    assert ">4</div>" not in empty.text
    assert f"city=msk" in empty.text
    assert f"season={SEASON}" in empty.text
    assert f"day={DAY}" in empty.text
    assert "ФОРУМ: ИТОГИ" in empty.text

    home = client.get("/", params={"city": "msk"})
    assert "Позвали сегодня" in home.text
    assert "ФОРУМ: ИТОГИ" in home.text
    assert 'href="/day?city=msk"' in home.text
    forum = client.get("/forum", params={"city": "msk"})
    assert "ФОРУМ: ИТОГИ" in forum.text
    assert 'href="/day?city=msk"' in forum.text
    feedback = client.get("/feedback", params={"city": "msk"})
    assert "<h1>ФОРУМ: ИТОГИ</h1>" in feedback.text
    assert 'href="/day?city=msk"' in feedback.text


def test_day_page_bound_city_ignores_foreign_arrival(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    path = _ready(tmp_path, "dashboard_day_bound.db")
    _run(_seed_cities())
    for tid in (11, 12, 13, 14):
        _run(_named(tid, f"Номер {tid}"))
        _run(_seed_entry(tid))

    client = tdr._stats_manager_client(path, city="spb")
    page = client.get("/day", params={"city": "msk", "day": DAY})
    assert page.status_code == 200
    assert "Отметок на входе ещё нет." in page.text
    assert ">4</div>" not in page.text
    assert "Ваш город:" in page.text
    assert 'aria-label="Город"' not in page.text


def test_day_page_hides_reserve_until_rules_on(tmp_path):
    from tests import test_dashboard_render as tdr

    path = _ready(tmp_path, "dashboard_day_rules_off.db")
    client = tdr._stats_manager_client(path)
    text = client.get("/day").text
    assert "Резерв виден, когда включены правила автоотказа." in text
    assert "Позвали" not in text


async def _program(day, title):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO program_sessions "
            "(city, day, start_time, end_time, title, created_at, updated_at) "
            "VALUES ('msk', ?, '10:00', '11:00', ?, ?, ?)",
            (day, title, f"{day} 09:00:00", f"{day} 09:00:00"),
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
