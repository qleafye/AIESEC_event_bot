"""Задача координатора 25.09 (дашборд `/forum`): часть агрегатов дашборд считает СВОИМИ SQL
в `dashboard/queries.py`, зеркалом функций бота (`database/db.py`/`services/applications/decision_delivery.py`).
Этот файл — ПАРИТЕТ-тесты: одна фикстурная БД с разнообразными данными (несколько городов,
прошлый И текущий сезон, одобренные/неодобренные, пустые ответы) → функция бота и запрос
дашборда дают ОДИНАКОВЫЕ числа.

Пары:
- `queries.noshow_poll_block` ↔ `database.db.forum_noshow_poll_summary`;
- `queries.regional_move_block` ↔ `database.db.regional_noshow_move_summary`;
- `queries.stats_card_block` ↔ `database.db.forum_stats_card_summary`;
- `queries.sos_block` (`by_day`) ↔ `database.db.sos_day_stats` (по дням);
- `queries.decision_delivery_block` ↔ `services.applications.decision_delivery.summarize_deliveries` над
  тем же списком пользователей, что читает `services.sheets.sheet_reconcile._current_season_users`.

Фикстура — прямые INSERT через `database.db._connect()` (aiosqlite), дашборд читает через
`sqlite3.connect(..., row_factory=sqlite3.Row)` — тот же приём, что
`tests/test_arrival_stats_260924.py::test_dashboard_block_matches_bot` и
`tests/test_dashboard_forum_stats_260926.py`.
"""
from __future__ import annotations

import asyncio
import sqlite3

import domain.cities as cities
import services.applications.decision_delivery as decision_delivery_service
from config import config
from database import db
from database.db import _connect
from services.sheets.sheet_reconcile import _current_season_users
from tests._dbtpl import fast_init_db
from tests.test_arrival_stats_260924 import _user

from dashboard import queries

ADMIN_ID = 926200
SEASON = "YL'26"
PAST_SEASON = "YL'25"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="dashboard_forum_parity.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))


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


async def _seed_noshow_poll(rows):
    """`rows` — `(telegram_id, city, season, reason)`."""
    async with _connect() as conn:
        for tid, city, season, reason in rows:
            await conn.execute(
                "INSERT INTO forum_noshow_poll (telegram_id, city, season, sent_at, reason) "
                "VALUES (?, ?, ?, '2026-10-04 09:00:00', ?)",
                (tid, city, season, reason),
            )
        await conn.commit()


async def _seed_regional_move(rows):
    """`rows` — `(telegram_id, source_city, season, response)`."""
    async with _connect() as conn:
        for tid, source_city, season, response in rows:
            await conn.execute(
                "INSERT INTO regional_noshow_move (telegram_id, source_city, season, sent_at, response) "
                "VALUES (?, ?, ?, '2026-10-04 09:00:00', ?)",
                (tid, source_city, season, response),
            )
        await conn.commit()


async def _seed_stats_card(rows):
    """`rows` — `(telegram_id, city, season)`."""
    async with _connect() as conn:
        for tid, city, season in rows:
            await conn.execute(
                "INSERT INTO forum_stats_card_sends (telegram_id, city, season, sent_at) "
                "VALUES (?, ?, ?, '2026-10-05 09:00:00')",
                (tid, city, season),
            )
        await conn.commit()


async def _seed_sos(rows):
    """`rows` — `(telegram_id, city, created_at, claimed_at, resolved_at)`."""
    async with _connect() as conn:
        for tid, city, created_at, claimed_at, resolved_at in rows:
            await conn.execute(
                "INSERT INTO sos_reports (telegram_id, city, created_at, claimed_at, resolved_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, city, created_at, claimed_at, resolved_at),
            )
        await conn.commit()


async def _seed_decision_delivery(rows):
    """`rows` — `(telegram_id, status, delivery_status, delivery_error)`."""
    async with _connect() as conn:
        for tid, status, delivery_status, delivery_error in rows:
            await conn.execute(
                "UPDATE users SET status = ?, decision_delivery_status = ?, "
                "decision_delivery_error = ? WHERE telegram_id = ?",
                (status, delivery_status, delivery_error, tid),
            )
        await conn.commit()


# ── опрос неявившихся: noshow_poll_block ↔ forum_noshow_poll_summary ────────────────────────

def test_noshow_poll_parity_matches_bot_with_and_without_city(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_seed_noshow_poll([
        (1, "spb", SEASON, "far"),
        (2, "spb", SEASON, "forgot"),
        (3, "spb", SEASON, None),  # ещё не ответил
        (4, "msk", SEASON, "changed_mind"),
        (5, "spb", PAST_SEASON, "other"),  # прошлый сезон — вне текущего скоупа
    ]))
    conn = _conn()
    try:
        for city in (None, "spb", "msk"):
            scope = cities.city_scope(city)
            bot_summary = _run(db.forum_noshow_poll_summary(SEASON, city_scope=scope))
            dash_block = queries.noshow_poll_block(conn, queries.Scope(city=city, season=SEASON))
            assert dash_block is not None or bot_summary["sent"] == 0
            if dash_block is None:
                assert bot_summary["sent"] == 0
                continue
            assert dash_block["sent"] == bot_summary["sent"], city
            assert dash_block["answered"] == bot_summary["answered"], city
            by_reason_dash = {
                code: next(
                    (r["count"] for r in dash_block["by_reason"]
                     if r["label"] == queries._NOSHOW_REASON_LABELS[code]),
                    0,
                )
                for code in bot_summary["by_reason"]
            }
            assert by_reason_dash == bot_summary["by_reason"], city
    finally:
        conn.close()


def test_noshow_poll_parity_empty_db(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        bot_summary = _run(db.forum_noshow_poll_summary(SEASON, city_scope=None))
        dash_block = queries.noshow_poll_block(conn, queries.Scope(season=SEASON))
        assert bot_summary["sent"] == 0
        assert dash_block is None
    finally:
        conn.close()


# ── перенос регионов: regional_move_block ↔ regional_noshow_move_summary ───────────────────

def test_regional_move_parity_matches_bot_with_and_without_city(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_seed_regional_move([
        (1, "spb", SEASON, "moved"),
        (2, "spb", SEASON, "declined"),
        (3, "spb", SEASON, None),  # ещё не ответил
        (4, "msk", SEASON, "moved"),
        (5, "spb", PAST_SEASON, "moved"),  # прошлый сезон
    ]))
    conn = _conn()
    try:
        for city in (None, "spb", "msk"):
            scope = cities.city_scope(city)
            bot_summary = _run(db.regional_noshow_move_summary(SEASON, city_scope=scope))
            dash_block = queries.regional_move_block(conn, queries.Scope(city=city, season=SEASON))
            if bot_summary["offered"] == 0:
                assert dash_block is None, city
                continue
            assert dash_block == bot_summary, city
    finally:
        conn.close()


def test_regional_move_parity_empty_db(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        bot_summary = _run(db.regional_noshow_move_summary(SEASON, city_scope=None))
        dash_block = queries.regional_move_block(conn, queries.Scope(season=SEASON))
        assert bot_summary["offered"] == 0
        assert dash_block is None
    finally:
        conn.close()


# ── «Юлид в цифрах»: stats_card_block ↔ forum_stats_card_summary ───────────────────────────

def test_stats_card_parity_matches_bot_with_and_without_city(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_seed_stats_card([
        (1, "spb", SEASON),
        (2, "spb", SEASON),
        (3, "msk", SEASON),
        (4, "spb", PAST_SEASON),  # прошлый сезон
    ]))
    conn = _conn()
    try:
        for city in (None, "spb", "msk"):
            scope = cities.city_scope(city)
            bot_summary = _run(db.forum_stats_card_summary(SEASON, city_scope=scope))
            dash_block = queries.stats_card_block(conn, queries.Scope(city=city, season=SEASON))
            if bot_summary["sent"] == 0:
                assert dash_block is None, city
                continue
            assert dash_block == bot_summary, city
    finally:
        conn.close()


def test_stats_card_parity_empty_db(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        bot_summary = _run(db.forum_stats_card_summary(SEASON, city_scope=None))
        dash_block = queries.stats_card_block(conn, queries.Scope(season=SEASON))
        assert bot_summary["sent"] == 0
        assert dash_block is None
    finally:
        conn.close()


# ── SOS: sos_block.by_day ↔ sos_day_stats (по дням, с/без города) ──────────────────────────
# `sos_day_stats` (бот, отчёт конкретного дня форума) не принимает сезон вовсе — вопрос
# «сколько SOS в ЭТОТ день» не сезонный по своей природе (за один день форума живёт ровно
# один сезон на практике). Сравнение поэтому держит все SOS-строки в ОДНОМ (текущем) сезоне —
# сравнивать с подмешанным прошлым сезоном на дашборде (который сезон фильтрует через JOIN
# users) было бы сравнением разных вопросов, не расхождением.

def test_sos_day_parity_matches_bot_per_day_with_and_without_city(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(1, city="spb", season=SEASON))
    _run(_user(2, city="spb", season=SEASON))
    _run(_user(3, city="msk", season=SEASON))
    _run(_seed_sos([
        (1, "spb", "2026-10-03 10:00:00", "2026-10-03 10:10:00", "2026-10-03 10:20:00"),
        (2, "spb", "2026-10-03 11:00:00", "2026-10-03 11:30:00", None),
        (3, "msk", "2026-10-03 12:00:00", None, None),
        (1, "spb", "2026-10-04 09:00:00", "2026-10-04 09:05:00", "2026-10-04 09:10:00"),
    ]))
    conn = _conn()
    try:
        for city in (None, "spb", "msk"):
            scope = cities.city_scope(city)
            dash_block = queries.sos_block(conn, queries.Scope(city=city, season=SEASON))
            by_day_dash = {row["day"]: row["count"] for row in dash_block["by_day"]}
            for day, day_label in (("2026-10-03", "03.10 (сб)"), ("2026-10-04", "04.10 (вс)")):
                bot_day = _run(db.sos_day_stats(day, city_scope=scope))
                assert by_day_dash.get(day_label, 0) == bot_day["total"], (city, day)
    finally:
        conn.close()


def test_sos_day_parity_totals_and_avg_claim_match_bot(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(1, city="spb", season=SEASON))
    _run(_user(2, city="spb", season=SEASON))
    _run(_seed_sos([
        (1, "spb", "2026-10-03 10:00:00", "2026-10-03 10:10:00", "2026-10-03 10:20:00"),
        (2, "spb", "2026-10-03 11:00:00", "2026-10-03 11:30:00", None),
    ]))
    conn = _conn()
    try:
        dash_block = queries.sos_block(conn, queries.Scope(city="spb", season=SEASON))
        bot_day = _run(db.sos_day_stats("2026-10-03", city_scope=cities.city_scope("spb")))
        assert dash_block["total"] == bot_day["total"]
        assert dash_block["resolved"] == bot_day["resolved"]
        assert dash_block["avg_claim_minutes"] == (
            round(bot_day["avg_claim_minutes"], 1) if bot_day["avg_claim_minutes"] is not None else None
        )
    finally:
        conn.close()


def test_sos_day_parity_empty_db(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        assert queries.sos_block(conn, queries.Scope()) is None
        bot_day = _run(db.sos_day_stats("2026-10-03", city_scope=None))
        assert bot_day["total"] == 0
    finally:
        conn.close()


# ── доставка решений: decision_delivery_block ↔ services.applications.decision_delivery.summarize_deliveries

def test_decision_delivery_parity_matches_bot_with_and_without_city(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(1, city="spb", season=SEASON, status="approved"))
    _run(_user(2, city="spb", season=SEASON, status="approved"))
    _run(_user(3, city="spb", season=SEASON, status="rejected"))
    _run(_user(4, city="msk", season=SEASON, status="rejected"))
    _run(_user(5, city="spb", season=SEASON, status="pending"))  # не решено
    _run(_user(6, city="spb", season=PAST_SEASON, status="approved"))  # прошлый сезон
    _run(_seed_decision_delivery([
        (1, "approved", "delivered", None),
        (2, "approved", "failed", decision_delivery_service.ERROR_BLOCKED),
        (3, "rejected", "failed", "чат не найден"),
        (4, "rejected", "queued", None),
        (6, "approved", "delivered", None),
    ]))
    conn = _conn()
    try:
        for city in (None, "spb", "msk"):
            scope = cities.city_scope(city)
            users = _run(_current_season_users(city_scope=scope))
            decided = [u for u in users if u.get("status") in decision_delivery_service.DECIDED_STATUSES]
            summary = decision_delivery_service.summarize_deliveries(users)
            delivered = [
                u for u in decided if u.get("decision_delivery_status") == "delivered"
            ]
            expected = {
                "total": len(decided),
                "delivered": len(delivered),
                "failed": len(summary["failed"]),
                "blocked": len(summary["blocked"]),
                "resendable": len(summary["resendable"]),
                "queued": len(summary["queued"]),
                "unknown": len(summary["unknown"]),
            }
            dash_block = queries.decision_delivery_block(conn, queries.Scope(city=city, season=SEASON))
            if expected["total"] == 0:
                assert dash_block is None, city
                continue
            assert dash_block == expected, city
    finally:
        conn.close()


def test_decision_delivery_parity_empty_db(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        users = _run(_current_season_users(city_scope=None))
        assert users == []
        dash_block = queries.decision_delivery_block(conn, queries.Scope())
        assert dash_block is None
    finally:
        conn.close()
