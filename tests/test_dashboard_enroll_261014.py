"""Раздел «Запись на сессии» страницы «Форум» дашборда: цифры совпадают со вкладкой Google-таблицы,
городской менеджер видит только свой город."""
from __future__ import annotations

import sqlite3

from config import config
from database import db, session_enroll_db as edb
from tests._enroll38 import DAY, SEASON, add_user, ready, run, seed_msk_program
from tests.test_enroll_sheet_261014 import _scan

from dashboard import queries


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def _seed():
    """3 одобренных (101, 102 записаны, 101 — на две сессии), 1 неодобренный записан."""
    await db.set_setting("event_season", SEASON)
    ids = await seed_msk_program()
    for tid in (101, 102, 103):
        await add_user(tid)
    await add_user(104, status="pending")
    await edb.enroll_tx(101, ids["A"])
    await edb.enroll_tx(101, ids["D"])
    await edb.enroll_tx(102, ids["A"])
    await edb.enroll_tx(104, ids["B"])
    await db.update_program_session(ids["A"], enroll_limit=5)
    await _scan(101, ids["A"])
    return ids


def test_empty_base_gives_none(tmp_path):
    ready(tmp_path)
    with _conn() as conn:
        assert queries.enroll_block(conn, queries.Scope()) is None


def test_block_counts(tmp_path):
    ready(tmp_path)
    ids = run(_seed())
    with _conn() as conn:
        block = queries.enroll_block(conn, queries.Scope())
    assert (block["enrolled"], block["approved"], block["pct"]) == (2, 3, 67)
    assert block["has_arrived"] is True
    by_id = {s["id"]: s for s in block["sessions"]}
    assert ids["P"] not in by_id
    a = by_id[ids["A"]]
    assert (a["enrolled"], a["enroll_limit"], a["free"], a["arrived"]) == (2, 5, 3, 1)
    assert a["time"] == "10:00–11:00" and a["city_label"] == "msk"
    assert by_id[ids["B"]]["enrolled"] == 1 and by_id[ids["B"]]["free"] is None


def test_block_city_scope(tmp_path):
    ready(tmp_path)
    run(_seed())
    with _conn() as conn:
        assert queries.enroll_block(conn, queries.Scope(city="spb")) is None
        msk = queries.enroll_block(conn, queries.Scope(city="msk"))
    assert len(msk["sessions"]) == 4


def _forum(tmp_path, monkeypatch, name, *, city=None, seed=True):
    from tests import test_dashboard_render as tdr

    db_path = tdr._use_tmp_db(tmp_path, name)
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    if seed:
        run(_seed())
    else:
        run(db.set_setting("event_season", SEASON))
    return tdr._stats_manager_client(db_path, city=city).get("/forum")


def test_forum_page_shows_section(tmp_path, monkeypatch):
    resp = _forum(tmp_path, monkeypatch, "forum_enroll.db")
    assert resp.status_code == 200
    assert 'id="enroll"' in resp.text and 'href="#enroll"' in resp.text
    assert "Записались хотя бы на одну сессию: 2 из 3 одобренных (67%)" in resp.text
    assert "Сессия A" in resp.text


def test_forum_page_empty_enroll(tmp_path, monkeypatch):
    resp = _forum(tmp_path, monkeypatch, "forum_enroll_empty.db", seed=False)
    assert resp.status_code == 200
    assert 'id="enroll"' in resp.text
    assert "сессий, на которые можно записаться, пока нет" in resp.text


def test_forum_page_bound_city_manager(tmp_path, monkeypatch):
    own = _forum(tmp_path, monkeypatch, "forum_enroll_msk.db", city="msk")
    assert "Сессия A" in own.text
    other = _forum(tmp_path, monkeypatch, "forum_enroll_spb.db", city="spb")
    assert "Сессия A" not in other.text
