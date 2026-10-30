"""Страница дашборда «Опросы и фидбек»: агрегат `queries.feedback_day` и периметр `/feedback`.

Узкий тест: открытый опрос бота с аудиторией «пришли сегодня» + город, завершившие vs начавшие,
чужой город не попадает, регистрационная внешняя форма и вчерашний native-poll не входят.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3

from config import config
from database import db
from database.db import _connect
from database import survey_db as sdb
from tests._dbtpl import fast_init_db
from tests.test_arrival_stats_260924 import _user

from dashboard import queries

ADMIN_ID = 930101
SEASON = "YL 26/2"
DAY = "2026-10-30"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="dashboard_feedback.db"):
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


async def _seed_survey():
    audience = json.dumps([
        {"field": "checkin_entry", "value": "yes", "day": DAY},
        {"field": "event_city", "value": "msk", "exclude": ["spb"], "label": "Москва"},
    ], ensure_ascii=False)
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, "
            "created_at, city, audience_json, status, scheduled_at, sending_since, kind) "
            "VALUES ('Как старт', '[]', 0, 0, ?, 'msk', ?, 'open', ?, ?, 'survey')",
            (f"{DAY} 09:00:00", audience, f"{DAY} 12:30:00", f"{DAY} 12:30:00"),
        )
        pid = cur.lastrowid
        await conn.execute(
            "INSERT INTO poll_messages (poll_id, chat_id, status) VALUES (?, 1, 'ok'), (?, 2, 'ok')",
            (pid, pid),
        )
        await conn.execute(
            "INSERT INTO survey_progress "
            "(poll_id, user_id, current_qid, state, started_at, finished_at) VALUES "
            "(?, 1, 1, 'done', ?, ?), "
            "(?, 2, 1, 'active', ?, NULL)",
            (pid, f"{DAY} 12:40:00", f"{DAY} 12:41:00", pid, f"{DAY} 12:50:00"),
        )
        await conn.commit()
        return pid


async def _seed_old_poll():
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, "
            "created_at, city, audience_json, status, scheduled_at, sending_since, kind) "
            "VALUES ('Придёшь?', '[]', 0, 0, '2026-10-25 10:00:00', 'msk', '[]', "
            "'open', '2026-10-25 10:00:00', '2026-10-25 10:00:00', 'poll')"
        )
        await conn.commit()


async def _seed_reg_form():
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO external_forms (platform, external_id, title, created_at, status) "
            "VALUES ('yandex', 'abc', 'Яндекс Форма', '2026-10-01 00:00:00', 'active')"
        )
        fid = cur.lastrowid
        await conn.execute(
            "INSERT INTO external_form_columns (form_id, qkey, label, position) VALUES "
            "(?, 'fio', 'ФИО', 1), (?, 'uni', 'Название университета', 2)",
            (fid, fid),
        )
        await conn.commit()


def test_feedback_day_counts_open_survey_audience_and_progress(tmp_path, monkeypatch):
    _ready(tmp_path)
    from datetime import datetime as dt
    monkeypatch.setattr("dashboard.queries.msk_now", lambda: dt(2026, 10, 30, 19, 40, 0))
    _run(_seed_cities())
    _run(_user(1, city="msk", season=SEASON, status="approved"))
    _run(_user(2, city="msk", season=SEASON, status="approved"))
    _run(_user(3, city="msk", season=SEASON, status="approved"))
    _run(_seed_entry(1))
    _run(_seed_entry(2))
    _run(_seed_survey())
    _run(_seed_old_poll())
    _run(_seed_reg_form())

    conn = _conn()
    try:
        block = queries.feedback_day(conn, queries.Scope(city="msk", season=SEASON), day=DAY)
        empty = queries.feedback_day(conn, queries.Scope(city="spb", season=SEASON), day=DAY)
    finally:
        conn.close()

    assert block["arrived"] == 2
    assert block["totals"]["forms"] == 1
    form = block["forms"][0]
    assert form["type"] == "опрос бота"
    assert form["should"] == 2
    assert form["done"] == 1
    assert form["started"] == 2
    assert form["not_started"] == 0
    assert form["abandoned"] == 1
    assert form["pct"] == 50.0
    assert empty["totals"]["forms"] == 0
    assert any("Внешних форм" in n for n in block["notes"])
    assert any("Фидбек сессий" in n for n in block["notes"])
    names = [f["name"] for f in block["forms"]]
    assert "Придёшь?" not in names
    assert "Яндекс Форма" not in names


def test_feedback_page_requires_login_and_renders(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    db_path = tdr._use_tmp_db(tmp_path, "feedback_page.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))

    guest = tdr._client(tdr._cfg(db_path))
    assert guest.get("/feedback", follow_redirects=False).status_code in (302, 307)
    json_guest = guest.get("/feedback.json", follow_redirects=False)
    assert json_guest.status_code == 401

    client = tdr._stats_manager_client(db_path)
    resp = client.get("/feedback")
    assert resp.status_code == 200
    assert "Опросы и фидбек" in resp.text
    assert "Оценки сессий" in resp.text
    assert "Скачать сводку CSV" in resp.text
    payload = client.get("/feedback.json").json()
    assert "totals" in payload and "arrived" in payload
    assert payload["totals"]["forms"] == 0
    assert "sessions_totals" in payload
    csv_guest = guest.get("/feedback.csv", follow_redirects=False)
    assert csv_guest.status_code == 401
    csv_ok = client.get("/feedback.csv")
    assert csv_ok.status_code == 200
    assert csv_ok.content.startswith(b"\xef\xbb\xbf")
    assert "Форма" in csv_ok.content.decode("utf-8-sig")
    answers = client.get("/feedback.csv", params={"part": "answers"})
    assert answers.status_code == 200
    assert answers.content.startswith(b"\xef\xbb\xbf")


async def _seed_survey_questions(pid):
    q1 = await sdb.add_survey_question(
        pid, "single", "Какой сейчас вайб?",
        options=[{"t": "Драйв", "more": None}, {"t": "Усталость", "more": None}],
    )
    q2 = await sdb.add_survey_question(pid, "text", "Что хочешь увидеть?")
    await sdb.upsert_survey_answer(pid, 1, q1, "", 0)
    await sdb.upsert_survey_answer(pid, 1, q2, "", "Больше кейсов")
    await sdb.upsert_survey_answer(pid, 2, q1, "", 1)
    return q1, q2


async def _seed_session_with_ratings():
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO program_halls (city, name, capacity, sort_order) "
            "VALUES ('msk', 'Большой зал', 400, 1)"
        )
        cur = await conn.execute(
            "INSERT INTO program_sessions "
            "(city, day, start_time, end_time, title, hall_id, created_at, updated_at) "
            "VALUES ('msk', ?, '13:30', '14:00', 'Рынок меняется', 1, ?, ?)",
            (DAY, f"{DAY} 09:00:00", f"{DAY} 09:00:00"),
        )
        sid = cur.lastrowid
        await conn.execute(
            "INSERT INTO program_sessions "
            "(city, day, start_time, end_time, title, hall_id, created_at, updated_at) "
            "VALUES ('msk', ?, '15:30', '16:20', 'Резюме без опыта', 1, ?, ?)",
            (DAY, f"{DAY} 09:00:00", f"{DAY} 09:00:00"),
        )
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (1, ?, ?, 'scan', ?, ?), (2, ?, ?, 'scan', ?, ?)",
            (f"session:{sid}", f"{DAY} 13:40:00", f"{DAY} 13:40:00", DAY,
             f"session:{sid}", f"{DAY} 13:41:00", f"{DAY} 13:41:00", DAY),
        )
        await conn.execute(
            "INSERT INTO session_feedback "
            "(telegram_id, session_id, rating, comment, prompted_at, rated_at, commented_at) "
            "VALUES "
            "(1, ?, 5, 'Сильный спикер', ?, ?, ?), "
            "(2, ?, 3, NULL, ?, ?, NULL)",
            (sid, f"{DAY} 14:05:00", f"{DAY} 14:06:00", f"{DAY} 14:07:00",
             sid, f"{DAY} 14:08:00", f"{DAY} 14:09:00"),
        )
        await conn.commit()
        return sid


def test_feedback_day_question_distribution_and_session_ratings_even_if_off(tmp_path, monkeypatch):
    """Разбивка опроса по вопросам как в админке; оценки сессий видны при выключенном тумблере."""
    _ready(tmp_path, "feedback_questions.db")
    from datetime import datetime as dt
    monkeypatch.setattr("dashboard.queries.msk_now", lambda: dt(2026, 10, 30, 19, 40, 0))
    _run(_seed_cities())
    _run(_user(1, city="msk", season=SEASON, status="approved"))
    _run(_user(2, city="msk", season=SEASON, status="approved"))
    _run(_seed_entry(1))
    _run(_seed_entry(2))
    pid = _run(_seed_survey())
    q1, q2 = _run(_seed_survey_questions(pid))
    sid = _run(_seed_session_with_ratings())

    conn = _conn()
    try:
        block = queries.feedback_day(conn, queries.Scope(city="msk", season=SEASON), day=DAY)
        data, name = queries.feedback_csv(conn, queries.Scope(city="msk", season=SEASON), part="answers", day=DAY)
        summary, sname = queries.feedback_csv(conn, queries.Scope(city="msk", season=SEASON), part="summary", day=DAY)
    finally:
        conn.close()

    form = next(f for f in block["forms"] if f["kind"] == "survey")
    q_single = next(q for q in form["questions"] if q["id"] == q1)
    q_text = next(q for q in form["questions"] if q["id"] == q2)
    assert q_single["type"] == "single"
    assert q_single["options"][0]["label"] == "Драйв"
    assert q_single["options"][0]["count"] == 1
    assert q_single["options"][1]["count"] == 1
    assert q_single["options"][0]["pct"] == 50.0
    assert q_text["type"] == "text"
    assert q_text["respondents"] == 1
    assert "Больше кейсов" not in json.dumps(form["questions"], ensure_ascii=False)

    sessions = block["sessions"]
    assert len(sessions) == 2
    rated = next(s for s in sessions if s["id"] == sid)
    empty = next(s for s in sessions if s["id"] != sid)
    assert rated["hall"] == "Большой зал"
    assert rated["prompted"] == 2
    assert rated["done"] == 2
    assert rated["avg"] == 4.0
    assert rated["comments"] == 1
    stars = {o["label"]: o["count"] for o in rated["questions"][0]["options"]}
    assert stars["5"] == 1 and stars["3"] == 1
    assert empty["done"] == 0 and empty["prompted"] == 0
    assert block["sessions_totals"]["rated"] == 2
    assert block["sessions_totals"]["enabled"] is False
    assert any("тумблер выключен" in n for n in block["notes"])

    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "Тест 1" not in text
    assert "Больше кейсов" in text
    assert "Сильный спикер" in text
    assert "Драйв" in text
    assert name.endswith(".csv")
    summary_text = summary.decode("utf-8-sig")
    assert "Рынок меняется" in summary_text
    assert "Начали" in summary_text
    rows = list(csv.reader(io.StringIO(summary_text), delimiter=";"))
    assert any("Форма" in r[0] for r in rows if r)
    assert sname.startswith("опросы-")


def test_feedback_page_shows_questions_and_session_block(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    db_path = tdr._use_tmp_db(tmp_path, "feedback_questions_page.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("event_city_enabled", "on"))
    _run(_seed_cities())
    _run(_user(1, city="msk", season=SEASON, status="approved"))
    _run(_user(2, city="msk", season=SEASON, status="approved"))
    _run(_seed_entry(1))
    _run(_seed_entry(2))
    pid = _run(_seed_survey())
    _run(_seed_survey_questions(pid))
    _run(_seed_session_with_ratings())

    client = tdr._stats_manager_client(db_path)
    html = client.get("/feedback").text
    assert "Какой сейчас вайб?" in html
    assert "Драйв" in html
    assert "Больше кейсов" not in html
    assert "Оценки сессий" in html
    assert "Рынок меняется" in html
    assert "Большой зал" in html
    assert "Сильный спикер" not in html
    payload = client.get("/feedback.json").json()
    assert payload["sessions_totals"]["rated"] == 2
    csv_body = client.get("/feedback.csv", params={"part": "answers"}).text
    assert "Больше кейсов" in csv_body
    assert "Сильный спикер" in csv_body
