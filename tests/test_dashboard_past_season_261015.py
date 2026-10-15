"""Плитка «Были в прошлом сезоне» на главной дашборда (квик 261015-dashpast)."""
from __future__ import annotations

import asyncio

from config import config
from database import db as bot_db
from dashboard import db as dash_db
from dashboard.queries import Scope, past_season_block
from tests._dbtpl import fast_init_db

CUR = "YL 26/2"
OLD = "YL 26/1"


def _use_tmp_db(tmp_path) -> str:
    path = str(tmp_path / "past_season.db")
    config.DB_PATH = path
    fast_init_db()
    return path


async def _seed_async(users, settings=None, cities=None):
    async with bot_db._connect() as conn:
        for code, label, order in cities or []:
            await conn.execute(
                "INSERT INTO cities (code, label, enabled, sort_order, created_at) "
                "VALUES (?, ?, 1, ?, ?)", (code, label, order, "2026-01-01 00:00:00"))
        for key, value in (settings if settings is not None else {"event_season": CUR}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        for i, row in enumerate(users, start=1):
            row = {"telegram_id": 1000 + i, "full_name": f"U{i}", "status": "approved", **row}
            cols = ", ".join(row)
            await conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()))
        await conn.commit()


def _block(path, scope=None):
    with dash_db.read_conn(path) as conn:
        return past_season_block(conn, scope or Scope())


def test_not_returned_and_returned(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async([
        {"season": OLD}, {"season": OLD},
        {"season": OLD, "status": "rejected"},
        {"season": CUR, "prev_season": OLD},
        {"season": CUR},
    ]))
    assert _block(path) == {"not_returned": 2, "returned": 1, "past_season": OLD}


def test_null_season_counts_as_current(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async([{"season": None}, {"season": OLD}]))
    assert _block(path) == {"not_returned": 1, "returned": 0, "past_season": OLD}


def test_none_without_event_season(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async([{"season": OLD}], settings={"other": "x"}))
    assert _block(path) is None


def test_none_when_no_past_seasons(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async([{"season": CUR}, {"season": None}]))
    assert _block(path) is None


def test_none_when_past_season_in_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async([{"season": OLD}]))
    assert _block(path, Scope(season=OLD)) is None
    assert _block(path, Scope(season=CUR)) is not None


def test_city_scope_matches_event_city_or_text_city(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async(
        [
            {"season": OLD, "event_city": None, "city": "Москва и МО"},
            {"season": OLD, "event_city": "msk", "city": "Москва"},
            {"season": OLD, "event_city": None, "city": "Казань"},
            {"season": OLD, "event_city": "spb", "city": "Санкт-Петербург"},
            {"season": CUR, "prev_season": OLD, "event_city": "msk", "city": "Москва"},
            {"season": CUR, "prev_season": OLD, "event_city": "spb", "city": "Санкт-Петербург"},
        ],
        cities=[("msk", "Москва", 1), ("spb", "Санкт-Петербург", 2)],
    ))
    assert _block(path, Scope(city="msk")) == {
        "not_returned": 2, "returned": 1, "past_season": OLD}
    assert _block(path, Scope(city="spb")) == {
        "not_returned": 1, "returned": 1, "past_season": OLD}
    assert _block(path)["not_returned"] == 4


def test_tile_rendered_on_current_season_only(tmp_path):
    from tests import test_dashboard_render as r

    db_path = r._use_tmp_db(tmp_path)
    r._seed(
        settings={"event_season": CUR},
        users=[
            {"telegram_id": 5001, "full_name": "Старый", "status": "approved", "season": OLD,
             "registration_date": "2026-01-01 10:00:00"},
            {"telegram_id": 5002, "full_name": "Вернулся", "status": "approved", "season": CUR,
             "prev_season": OLD, "registration_date": "2026-09-01 10:00:00"},
        ],
    )
    client = r._stats_manager_client(db_path)
    current = client.get("/")
    assert current.status_code == 200
    assert "Были в прошлом сезоне" in current.text
    assert "ещё не подали анкету · вернулись: 1" in current.text
    past = client.get("/", params={"season": OLD})
    assert past.status_code == 200
    assert "Были в прошлом сезоне" not in past.text


def test_city_label_with_date_uses_base_name(tmp_path):
    path = _use_tmp_db(tmp_path)
    asyncio.run(_seed_async(
        [
            {"season": OLD, "event_city": None, "city": "Москва и МО"},
            {"season": OLD, "event_city": None, "city": "Санкт-Петербург"},
        ],
        cities=[("msk", "Москва, 30-31 октября", 1), ("spb", "Санкт-Петербург, 3 октября", 2)],
    ))
    assert _block(path, Scope(city="msk"))["not_returned"] == 1
    assert _block(path, Scope(city="spb"))["not_returned"] == 1
