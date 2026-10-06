"""Задача 25.09: страница дашборда «🎪 Форум» — сессии/оценки, SOS, решения по заявкам,
опросы после форума. Приход/стойки (переехали с главной страницы) уже покрыты
`tests/test_arrival_stats_260924.py`/`tests/test_arrival_floor_260925.py` (обновлены здесь же
задачей — маршрут сменился на `/forum`), этот файл их не дублирует.

Все новые агрегаты — read-only sqlite3 (`dashboard/queries.py`), без ПД (D-17): SOS и решения
по заявкам отдают только счётчики. Фикстура — прямые INSERT через `database.db._connect()`
(aiosqlite), читает — `sqlite3.connect(..., row_factory=sqlite3.Row)`, тот же приём, что
`tests/test_arrival_stats_260924.py::test_dashboard_block_matches_bot`.
"""
from __future__ import annotations

import asyncio
import sqlite3

import services.decision_delivery as decision_delivery_service
from config import config
from database import db
from database.db import _connect
from tests._dbtpl import fast_init_db
from tests.test_arrival_stats_260924 import _user

from dashboard import queries

ADMIN_ID = 926100
SEASON = "YL'26"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="dashboard_forum_stats.db"):
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


async def _seed_session_feedback(rows):
    """`rows` — `(telegram_id, session_id, rating, comment)`."""
    async with _connect() as conn:
        for tid, sid, rating, comment in rows:
            await conn.execute(
                "INSERT INTO session_feedback (telegram_id, session_id, rating, comment, prompted_at) "
                "VALUES (?, ?, ?, ?, '2026-10-03 12:00:00')",
                (tid, sid, rating, comment),
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


# ── session_ratings ──────────────────────────────────────────────────────────────────────

def test_session_ratings_aggregates_avg_count_and_comments(tmp_path):
    _ready(tmp_path)
    _run(_seed_session_feedback([
        (1, 10, 5, None),
        (2, 10, 3, "было хорошо"),
        (3, 10, None, "только текст"),
        (4, 20, 4, None),
    ]))
    conn = _conn()
    try:
        ratings = queries.session_ratings(conn)
    finally:
        conn.close()
    assert ratings[10]["avg"] == 4.0
    assert ratings[10]["count"] == 2
    assert ratings[10]["comments"] == 2
    assert ratings[20] == {"avg": 4.0, "count": 1, "comments": 0}


def test_session_ratings_empty_table_returns_empty_dict(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        assert queries.session_ratings(conn) == {}
    finally:
        conn.close()


# ── sos_block ────────────────────────────────────────────────────────────────────────────

def test_sos_block_counts_resolved_and_avg_claim_time(tmp_path):
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
        block = queries.sos_block(conn, queries.Scope(city="spb"))
    finally:
        conn.close()
    assert block["total"] == 2
    assert block["resolved"] == 1
    assert block["unresolved"] == 1
    # (10 + 30) / 2 = 20 минут до «Беру».
    assert block["avg_claim_minutes"] == 20.0
    assert block["by_day"] == [{"day": "03.10 (сб)", "count": 2}]


def test_sos_block_none_without_reports(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        assert queries.sos_block(conn, queries.Scope()) is None
    finally:
        conn.close()


def test_sos_block_scoped_by_city_snapshot(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities())
    _run(_user(1, city="spb", season=SEASON))
    _run(_user(2, city="msk", season=SEASON))
    _run(_seed_sos([
        (1, "spb", "2026-10-03 10:00:00", None, None),
        (2, "msk", "2026-10-03 10:00:00", None, None),
    ]))
    conn = _conn()
    try:
        spb = queries.sos_block(conn, queries.Scope(city="spb"))
        msk = queries.sos_block(conn, queries.Scope(city="msk"))
        allc = queries.sos_block(conn, queries.Scope())
    finally:
        conn.close()
    assert spb["total"] == 1 and msk["total"] == 1 and allc["total"] == 2


# ── decision_delivery_block ──────────────────────────────────────────────────────────────

def test_decision_delivery_block_counts_by_status(tmp_path):
    _ready(tmp_path)
    _run(_user(1, city="spb", season=SEASON, status="approved"))
    _run(_user(2, city="spb", season=SEASON, status="approved"))
    _run(_user(3, city="spb", season=SEASON, status="rejected"))
    _run(_user(4, city="spb", season=SEASON, status="rejected"))
    _run(_user(5, city="spb", season=SEASON, status="pending"))  # не решено — не считается
    _run(_seed_decision_delivery([
        (1, "approved", "delivered", None),
        (2, "approved", "failed", decision_delivery_service.ERROR_BLOCKED),
        (3, "rejected", "failed", "чат не найден"),
        (4, "rejected", "queued", None),
    ]))
    conn = _conn()
    try:
        block = queries.decision_delivery_block(conn, queries.Scope(city="spb"))
    finally:
        conn.close()
    assert block["total"] == 4  # без pending
    assert block["delivered"] == 1
    assert block["failed"] == 2
    assert block["blocked"] == 1
    assert block["resendable"] == 1
    assert block["queued"] == 1
    assert block["unknown"] == 0


def test_decision_delivery_error_blocked_constant_matches_bot_service(tmp_path):
    """Сторож дрейфа: строка причины «заблокировал бота» в `dashboard/queries.py`
    (`_DECISION_ERROR_BLOCKED`, дашборд не импортирует `services/*` целиком) должна дословно
    совпадать с `services.decision_delivery.ERROR_BLOCKED` — иначе счётчик «blocked» тихо
    съедет в «resendable»."""
    assert queries._DECISION_ERROR_BLOCKED == decision_delivery_service.ERROR_BLOCKED


def test_decision_delivery_block_none_without_decided_users(tmp_path):
    _ready(tmp_path)
    _run(_user(1, city="spb", season=SEASON, status="pending"))
    conn = _conn()
    try:
        assert queries.decision_delivery_block(conn, queries.Scope()) is None
    finally:
        conn.close()


# ── noshow_poll_block / regional_move_block / stats_card_block ─────────────────────────────

def test_noshow_poll_block_sent_answered_and_reasons(tmp_path):
    _ready(tmp_path)
    _run(_seed_noshow_poll([
        (1, "spb", SEASON, "far"),
        (2, "spb", SEASON, "forgot"),
        (3, "spb", SEASON, None),  # ещё не ответил
    ]))
    conn = _conn()
    try:
        block = queries.noshow_poll_block(conn, queries.Scope(city="spb", season=SEASON))
    finally:
        conn.close()
    assert block["sent"] == 3
    assert block["answered"] == 2
    by_reason = {r["label"]: r["count"] for r in block["by_reason"]}
    assert by_reason["Далеко/дорого"] == 1
    assert by_reason["Забыл(а)"] == 1
    assert by_reason["Передумал(а)"] == 0


def test_noshow_poll_block_none_without_sends(tmp_path):
    _ready(tmp_path)
    conn = _conn()
    try:
        assert queries.noshow_poll_block(conn, queries.Scope(season=SEASON)) is None
    finally:
        conn.close()


def test_regional_move_block_offered_moved_declined(tmp_path):
    _ready(tmp_path)
    _run(_seed_regional_move([
        (1, "spb", SEASON, "moved"),
        (2, "spb", SEASON, "declined"),
        (3, "spb", SEASON, None),
    ]))
    conn = _conn()
    try:
        block = queries.regional_move_block(conn, queries.Scope(city="spb", season=SEASON))
    finally:
        conn.close()
    assert block == {"offered": 3, "moved": 1, "declined": 1}


def test_stats_card_block_sent_count(tmp_path):
    _ready(tmp_path)
    _run(_seed_stats_card([(1, "spb", SEASON), (2, "spb", SEASON)]))
    conn = _conn()
    try:
        block = queries.stats_card_block(conn, queries.Scope(city="spb", season=SEASON))
        none_block = queries.stats_card_block(conn, queries.Scope(city="msk", season=SEASON))
    finally:
        conn.close()
    assert block == {"sent": 2}
    assert none_block is None


# ── рендер страницы «/forum» ────────────────────────────────────────────────────────────

def test_forum_page_renders_all_sections_with_data(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    db_path = tdr._use_tmp_db(tmp_path, "forum_page_full.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))
    _run(_seed_cities())
    _run(_user(1, city="spb", season=SEASON, status="approved"))
    _run(_seed_sos([(1, "spb", "2026-10-03 10:00:00", "2026-10-03 10:05:00", None)]))
    _run(_seed_decision_delivery([(1, "approved", "delivered", None)]))
    _run(_seed_noshow_poll([(2, "spb", SEASON, "far")]))
    _run(_seed_regional_move([(3, "spb", SEASON, "moved")]))
    _run(_seed_stats_card([(1, "spb", SEASON)]))

    client = tdr._stats_manager_client(db_path)
    html_text = client.get("/forum").text
    assert html_text.count('id="sessions"') == 1
    assert 'id="sos"' in html_text and 'id="decisions"' in html_text and 'id="after"' in html_text
    assert "Решено" in html_text and "Доставлено" in html_text
    assert "Перенос регионов" in html_text and "«Мы в цифрах»" in html_text


def test_forum_page_renders_empty_state_without_crashing(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    db_path = tdr._use_tmp_db(tmp_path, "forum_page_empty.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))
    client = tdr._stats_manager_client(db_path)
    resp = client.get("/forum")
    assert resp.status_code == 200
    assert resp.text.count("Нет данных") >= 3


def test_forum_page_bound_city_manager_sees_only_own_city_sos(tmp_path, monkeypatch):
    from tests import test_dashboard_render as tdr

    db_path = tdr._use_tmp_db(tmp_path, "forum_page_bound.db")
    monkeypatch.setattr(config, "ADMIN_IDS", [tdr.ADMIN_ID])
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("event_city_enabled", "on"))
    _run(_seed_cities())
    _run(_user(1, city="spb", season=SEASON))
    _run(_user(2, city="msk", season=SEASON))
    _run(_seed_sos([
        (1, "spb", "2026-10-03 10:00:00", None, None),
        (2, "msk", "2026-10-03 10:00:00", None, None),
    ]))
    client = tdr._stats_manager_client(db_path, city="spb")
    html_text = client.get("/forum").text
    # Привязанный к СПб менеджер не видит свитчер города (D-10) и явно видит только «1» SOS.
    assert 'aria-label="Город"' not in html_text
    assert "Ваш город:" in html_text


# ── формула-инъекция (CSV прихода бот генерирует arrival_stats.csv_bytes — переиспользуется
# ботом, у дашборда своего экспорт-роута НЕТ: сторож tests/test_dashboard_routes.py::
# test_no_app_route_and_no_export_or_csv_route запрещает любой роут со словом csv/export в
# пути — см. отчёт исполнителя, кнопка выгрузки из задачи сознательно не реализована). ────────

def test_arrival_stats_sheet_safe_escapes_formula_prefixes():
    import arrival_stats

    assert arrival_stats._sheet_safe("=SUM(A1)") == "'=SUM(A1)"
    assert arrival_stats._sheet_safe("+1") == "'+1"
    assert arrival_stats._sheet_safe("-1") == "'-1"
    assert arrival_stats._sheet_safe("@mention") == "'@mention"
    assert arrival_stats._sheet_safe("Зал А") == "Зал А"
