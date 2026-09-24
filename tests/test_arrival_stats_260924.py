"""Бэклог чек-ина п.10: «📊 Статистика прихода» — общий модуль `arrival_stats.py`, экран бота
(`handlers/admin_checkin_stats.py`) и блок дашборда «Приход» (`dashboard.queries.arrival_block`)
считают одно и то же одними запросами.

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import csv
import io
import sqlite3

import arrival_stats
import cities
from config import config
from database import db
from database.db import _connect
from handlers import admin_checkin, admin_checkin_stats
from services import checkin as checkin_service
from services import checkin_arrival
from tests._dbtpl import fast_init_db

ADMIN_ID = 924100
SEASON = "YL'26"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_arrival_stats_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))


async def _user(tid, city="spb", status="approved", season=SEASON):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city) VALUES (?, ?, ?, ?, ?)",
            (tid, f"Тест {tid}", status, season, city),
        )
        await conn.commit()


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.sent = []
        self.docs = []
        self.edited = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **k):
        self.sent.append((text, reply_markup))

    async def edit_text(self, text=None, parse_mode=None, reply_markup=None, **k):
        self.edited.append((text, reply_markup))

    async def answer_document(self, document, caption=None, **k):
        self.docs.append((document, caption))


class _Cb:
    def __init__(self, data, uid=ADMIN_ID):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.answers = []
        self.bot = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _seed_spb_forum():
    """СПб: 4 одобренных (1 — прошлого сезона не в счёт, 1 pending не в счёт), двое пришли в
    первый день (один — CSV без времени), один пришёл только на сессию во второй день."""
    _run(db.set_setting("event_city_enabled", "on"))
    for tid in (1, 2, 3, 4):
        _run(_user(tid))
    _run(_user(5, status="pending"))
    _run(_user(6, season="YL'25"))
    _run(_user(7, city="msk"))
    hall = _run(db.create_program_hall("spb", "Зал А", 10))
    s1 = _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие", hall_id=hall))
    _run(db.create_program_session("spb", "2026-10-04", "10:00", "11:00", "Второй день"))
    _run(db.record_checkin(1, "entry", source="miniapp", scanned_at="2026-10-03 09:10:00"))
    _run(db.record_checkin(2, "entry", source="csv", scanned_at="2026-10-03 12:00:00", approx=True))
    _run(db.record_checkin(1, f"session:{s1}", source="miniapp", scanned_at="2026-10-03 10:05:00"))
    _run(db.record_checkin(3, "entry", source="auto_session", scanned_at="2026-10-04 10:02:00"))
    _run(db.record_checkin(1, f"session:{s1 + 1}", source="miniapp", scanned_at="2026-10-04 10:03:00"))
    return s1


def test_entry_point_literal_matches_service():
    assert arrival_stats.ENTRY_POINT == checkin_service.ENTRY_POINT


def test_build_merge_top_and_csv_pure():
    rep = arrival_stats.build_report(
        4, (2, 1),
        [("2026-10-03", 2, 2)],
        [(1, "spb", "2026-10-03", "10:00", "11:00", "=Опасное", "Зал", 10, 5, 0),
         (2, "spb", "2026-10-03", "12:00", "13:00", "Вторая", None, None, 7, 0)],
    )
    assert (rep["arrived"], rep["not_arrived"], rep["pct"], rep["approx"]) == (2, 2, 50, 1)
    assert rep["sessions"][0]["fill_pct"] == 50 and rep["sessions"][1]["fill_pct"] is None
    assert [s["id"] for s in arrival_stats.top_sessions(rep, 1)] == [2]
    assert rep["days"][0]["label"] == "03.10 (сб)"
    total = arrival_stats.merge_reports([rep, rep])
    assert (total["approved"], total["arrived"], total["pct"]) == (8, 4, 50)
    assert total["days"][0]["present"] == 4
    raw = arrival_stats.csv_bytes([("СПб", rep)], {"spb": "СПб"}, total)
    assert raw.startswith("\ufeff".encode("utf-8"))
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert ["СПб", "4", "2", "2", "50", "1"] in rows
    assert ["Итого", "8", "4", "4", "50", "2"] in rows
    assert any(r[:5] == ["СПб", "03.10 (сб)", "10:00–11:00", "Зал", "'=Опасное"] for r in rows)


def test_bot_report_counts_current_season_approved_by_day_and_session(tmp_path):
    _ready(tmp_path)
    s1 = _seed_spb_forum()
    _run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))

    text, kb = _run(admin_checkin_stats.render_stats(ADMIN_ID))
    assert "Статистика прихода</b> — " in text
    assert "пришли 3 из 4 (75%)" in text
    assert "Не пришли: 1" in text
    assert "У 1 отметок время примерное" in text
    assert "03.10 (сб): 2 · впервые 2" in text
    assert "04.10 (вс): 2 · впервые 1" in text  # пришедший вчера отметился на сессии
    assert "Открытие — 1 из 10 (10%)" in text
    assert _cbs(kb) == ["checkin_stats_refresh", "checkin_stats_csv", "admin_checkin"]

    rep = _run(checkin_arrival.arrival_report(cities.city_scope("spb"), "spb"))
    assert [s["id"] for s in rep["sessions"]] == [s1, s1 + 1]


def test_all_cities_lists_each_city_and_total(tmp_path):
    _ready(tmp_path)
    _seed_spb_forum()
    _run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    text, _kb = _run(admin_checkin_stats.render_stats(ADMIN_ID))
    assert "— Все города" in text
    assert "Итого:</b> пришли 3 из 5 (60%), не пришли 2" in text
    assert "Тюмень" not in text  # ни одобренных, ни отметок


def test_no_marks_yet_says_so(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    text, _kb = _run(admin_checkin_stats.render_stats(ADMIN_ID))
    assert "Отметок пока нет." in text
    assert "офлайн-сканером" in text


def test_csv_button_sends_document_and_refresh_edits(tmp_path):
    _ready(tmp_path)
    _seed_spb_forum()
    _run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))
    cb = _Cb("checkin_stats_csv")
    _run(admin_checkin_stats.checkin_stats_csv(cb))
    doc, _caption = cb.message.docs[0]
    assert doc.filename.startswith("prihod_") and doc.filename.endswith(".csv")
    assert "Открытие" in doc.data.decode("utf-8-sig")
    cb = _Cb("checkin_stats_refresh")
    _run(admin_checkin_stats.checkin_stats_refresh(cb))
    assert "Статистика прихода" in cb.message.edited[0][0]


def test_checkin_screen_button_only_for_moderate_reg(tmp_path):
    _ready(tmp_path)
    from handlers.admin_caps import role_caps_key
    volunteer = 924101
    _run(db.add_staff(volunteer, "stats_manager", ADMIN_ID))
    _run(db.set_setting(role_caps_key("stats_manager"), "checkin"))
    cb = _Cb("admin_checkin", volunteer)
    _run(admin_checkin.show_admin_checkin(cb))
    assert "checkin_stats" not in _cbs(cb.message.sent[0][1])
    cb = _Cb("admin_checkin", ADMIN_ID)
    _run(admin_checkin.show_admin_checkin(cb))
    assert _cbs(cb.message.sent[0][1])[0] == "checkin_stats"


def test_dashboard_block_matches_bot(tmp_path):
    _ready(tmp_path)
    _seed_spb_forum()
    from dashboard import queries
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        block = queries.arrival_block(conn, queries.Scope(city="spb"))
    finally:
        conn.close()
    assert (block["approved"], block["arrived"], block["not_arrived"], block["pct"]) == (4, 3, 1, 75)
    assert [d["present"] for d in block["days"]] == [2, 2]
    assert block["sessions"][0]["city_label"]


def test_dashboard_block_absent_without_marks(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    from dashboard import queries
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        assert queries.arrival_block(conn, queries.Scope()) is None
    finally:
        conn.close()


def test_dashboard_page_renders_arrival_section(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr
    db_path = tdr._use_tmp_db(tmp_path, "arrival_render.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))
    _seed_spb_forum()
    client = tdr._stats_manager_client(db_path)
    html_text = client.get("/").text
    assert 'id="arrival"' in html_text
    assert "Открытие" in html_text and "Явка" in html_text
