"""Бэклог чек-ина №12: «📍 Сейчас на площадке» — общий SQL `arrival_stats.floor_queries`, экран
бота (`handlers/admin_checkin_floor.py`) и блок дашборда в разделе «Приход»
(`dashboard.queries.arrival_floor`) считают одно и то же за ДЕНЬ форума (МСК)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import arrival_stats
from config import config
from database import db
from handlers import admin_checkin, admin_checkin_floor
from tests.test_arrival_stats_260924 import ADMIN_ID, SEASON, _Cb, _cbs, _ready, _run, _user

DAY = "2026-10-03"
NOW = datetime(2026, 10, 3, 10, 30, 0)


def _freeze(monkeypatch):
    monkeypatch.setattr(admin_checkin_floor, "msk_now", lambda: NOW)
    from dashboard import queries
    monkeypatch.setattr(queries, "msk_now", lambda: NOW)


def _seed():
    """СПб: 4 одобренных пришли сегодня (один — из файла сканера), трое — в последние 15 минут;
    Москва: один. Стойки: 500 (имя в журнале) — двое СПб, 501 (без имени) — СПб + Москва."""
    _run(db.set_setting("event_city_enabled", "on"))
    for tid in (1, 2, 3, 4):
        _run(_user(tid))
    _run(_user(7, city="msk"))
    _run(_user(8, status="pending"))
    hall = _run(db.create_program_hall("spb", "Зал А", 10))
    s1 = _run(db.create_program_session("spb", DAY, "10:00", "11:00", "Открытие", hall_id=hall))
    _run(db.create_program_session("spb", DAY, "12:00", "13:00", "Позже"))
    _run(db.record_checkin(1, "entry", source="miniapp", scanned_at=f"{DAY} 09:10:00", by_staff_id=500))
    _run(db.record_checkin(2, "entry", source="manual", scanned_at=f"{DAY} 10:20:00", by_staff_id=500))
    _run(db.record_checkin(3, "entry", source="miniapp", scanned_at=f"{DAY} 10:25:00", by_staff_id=501))
    _run(db.record_checkin(4, "entry", source="csv", scanned_at=f"{DAY} 10:22:00", by_staff_id=502, approx=True))
    _run(db.record_checkin(7, "entry", source="miniapp", scanned_at=f"{DAY} 10:21:00", by_staff_id=501))
    _run(db.record_checkin(1, f"session:{s1}", source="miniapp", scanned_at=f"{DAY} 10:05:00", by_staff_id=500))
    _run(db.venue_log_add({"action": "checkin", "staff_id": 500, "staff_name": "Анна (@anna)",
                           "telegram_id": 1, "city": "spb", "point": "entry", "source": "miniapp",
                           "details": {}}))
    return s1


def test_fill_buckets_dense_axis_until_now():
    assert arrival_stats.fill_buckets([("09:00", 1), ("09:45", 2)], "10:07") == [
        ("09:00", 1), ("09:15", 0), ("09:30", 0), ("09:45", 2), ("10:00", 0),
    ]
    assert arrival_stats.fill_buckets([]) == []


def test_bot_floor_for_bound_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    _seed()
    monkeypatch.setattr(admin_checkin_floor, "_admin_city_scope", _scope_spb)
    text, kb = _run(admin_checkin_floor.render_floor(ADMIN_ID))
    assert text.startswith("📍 <b>Сейчас на площадке</b> — ")
    assert "Сегодня пришли 4 из 4 (100%) одобренных" in text
    assert "За последние 15 мин: +3" in text
    assert "Открытие — отметились 1 из 10 (10%)" in text
    assert "Позже" not in text  # сессия не идёт сейчас
    assert "1. Анна (@anna) — 2" in text
    assert "2. id 501 — 1" in text  # москвич 501-го не в городе СПб; CSV-стойка 502 не в счёт
    assert "502" not in text
    assert _cbs(kb) == ["checkin_floor_refresh", "admin_checkin"]


async def _scope_spb(_admin_id):
    import cities
    return cities.city_scope("spb")


def test_bot_floor_all_cities_sums_and_counts_stands_across_cities(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    _seed()
    import cities
    _run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    text, _kb = _run(admin_checkin_floor.render_floor(ADMIN_ID))
    assert "Все города" in text
    assert "Сегодня пришли 5 из 5 (100%) одобренных" in text
    assert "За последние 15 мин: +4" in text
    assert "id 501 — 2" in text and "Анна (@anna) — 2" in text


def test_bot_floor_empty_day_says_so(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    _run(_user(1))
    text, _kb = _run(admin_checkin_floor.render_floor(ADMIN_ID))
    assert "Сегодня пришли 0 из 1 (0%) одобренных" in text
    assert "ещё никого не отметили" in text
    assert "ни одна сессия программы не идёт" in text


def test_refresh_edits_message(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    cb = _Cb("checkin_floor_refresh")
    _run(admin_checkin_floor.checkin_floor_refresh(cb))
    assert "Сейчас на площадке" in cb.message.edited[0][0]
    cb = _Cb("checkin_floor")
    _run(admin_checkin_floor.checkin_floor_open(cb))
    assert cb.message.sent


def test_checkin_screen_has_floor_button_next_to_stats_only_for_moderate_reg(tmp_path):
    _ready(tmp_path)
    from handlers.admin_caps import ADMIN_CAPS, role_caps_key
    assert ADMIN_CAPS["checkin_floor"] == "moderate_reg"
    assert ADMIN_CAPS["checkin_floor_refresh"] == "moderate_reg"
    cb = _Cb("admin_checkin", ADMIN_ID)
    _run(admin_checkin.show_admin_checkin(cb))
    first_row = cb.message.sent[0][1].inline_keyboard[0]
    assert [b.callback_data for b in first_row] == ["checkin_stats", "checkin_floor"]
    volunteer = 924102
    _run(db.add_staff(volunteer, "stats_manager", ADMIN_ID))
    _run(db.set_setting(role_caps_key("stats_manager"), "checkin"))
    cb = _Cb("admin_checkin", volunteer)
    _run(admin_checkin.show_admin_checkin(cb))
    assert "checkin_floor" not in _cbs(cb.message.sent[0][1])


def _dash(scope_city=None, day=None):
    from dashboard import queries
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        scope = queries.Scope(city=scope_city)
        block = queries.arrival_block(conn, scope)
        return queries.arrival_floor(conn, scope, block["days"], day)
    finally:
        conn.close()


def test_dashboard_floor_matches_bot_with_quarter_hour_bars(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    _seed()
    fl = _dash("spb")
    assert (fl["day"], fl["is_today"], fl["present"], fl["approved"], fl["recent"]) == (DAY, True, 4, 4, 3)
    assert fl["chart"]["labels"] == ["09:00", "09:15", "09:30", "09:45", "10:00", "10:15", "10:30"]
    assert fl["chart"]["counts"] == [1, 0, 0, 0, 0, 3, 0]
    assert fl["chart"]["cumulative"][-1] == 4
    assert fl["points"][0]["count"] == 4
    assert [p["live"] for p in fl["points"][1:]] == [True, False]
    assert [(s["name"], s["count"]) for s in fl["stands"]] == [("Анна (@anna)", 2), ("id 501", 1)]
    assert fl["cities"] == []  # выбран один город — среза по городам нет


def test_dashboard_floor_all_cities_has_city_slice(tmp_path, monkeypatch):
    _ready(tmp_path)
    _freeze(monkeypatch)
    _seed()
    conn = sqlite3.connect(config.DB_PATH)
    conn.executemany(
        "INSERT INTO cities (code, label, enabled, sort_order, created_at) VALUES (?, ?, 1, ?, '')",
        [("spb", "СПб", 1), ("msk", "Москва", 2)],
    )
    conn.commit()
    conn.close()
    fl = _dash()
    assert sorted((c["present"], c["approved"]) for c in fl["cities"]) == [(1, 1), (4, 4)]
    assert sum(c["present"] for c in fl["cities"]) == fl["present"] == 5


def test_dashboard_page_renders_floor_block(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr
    db_path = tdr._use_tmp_db(tmp_path, "arrival_floor_render.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))
    _freeze(monkeypatch)
    _seed()
    client = tdr._stats_manager_client(db_path)
    html_text = client.get("/").text
    assert "Сейчас на площадке" in html_text
    assert 'id="arrival-chart"' in html_text and "data-cumulative" in html_text
    assert "Стойки входа" in html_text and "Анна (@anna)" in html_text
