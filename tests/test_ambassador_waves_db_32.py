"""Phase 32 План 1 (D-01/D-08/D-10/D-12/D-14/D-15/D-22/D-27/D-28): вся работа с базой под
амбассадорский слой волнами — три новые таблицы, шесть новых колонок, аксессоры волн,
заданий, амбассадоров и начислений, сторож пожизненного рейтинга.

Три пласта сторожей — по одному на задачу плана:
- Задача 1: схема (`ambassador_waves`/`wave_results`/`referral_credits`, шесть колонок,
  константы) + предусловие «фаза 31 приехала».
- Задача 2: аксессоры волн, заданий, амбассадоров.
- Задача 3: аксессоры начислений + сторож пожизненного рейтинга (`get_leaderboard`/
  `get_user_rank` не изменились).

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_reject_rules_db.py::_ready(tmp_path)`.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from config import config
from database import db


def _ready(tmp_path, name="test_ambassador_waves_db_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, registration_date=None, referrer_id=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": registration_date or f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
        "referrer_id": referrer_id,
    }))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: схема — три таблицы, шесть колонок, константы, предусловие фазы 31
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_precondition_phase_31_landed(tmp_path):
    """Фаза 31 (тип настройки date_only + колонки автоотказа users) обязана быть в базе,
    иначе набор тестов должен падать с прямым человеческим объяснением, а не путаным
    KeyError где-то в середине другого теста (D-01)."""
    import settings_schema

    has_date_only = any(
        entry.get("type") == "date_only" for entry in settings_schema.SETTINGS_SCHEMA.values()
    )
    if not has_date_only:
        pytest.fail("Фаза 31 не приехала: сначала выполните фазу 31 (тип date_only, колонки автоотказа)")

    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    finally:
        con.close()
    if "auto_reject_rule_ids" not in cols:
        pytest.fail("Фаза 31 не приехала: сначала выполните фазу 31 (тип date_only, колонки автоотказа)")


def test_ambassador_tables_created(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    finally:
        con.close()
    assert {"ambassador_waves", "wave_results", "referral_credits"} <= tables


def test_ambassador_waves_columns(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(ambassador_waves)")}
    finally:
        con.close()
    expected = {
        "id", "number", "starts_at", "ends_at", "intro_text", "prize_places", "state",
        "event_city", "started_notified_at", "created_by", "created_at",
    }
    assert expected <= cols
    assert "name" not in cols
    assert "title" not in cols


def test_wave_results_columns(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(wave_results)")}
    finally:
        con.close()
    assert {"wave_id", "user_id", "place", "points", "announced_at"} <= cols


def test_referral_credits_columns(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(referral_credits)")}
    finally:
        con.close()
    assert {"invitee_id", "referrer_id", "coins", "wave_id", "credited_at", "source"} <= cols


def test_six_new_columns_present(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        game_tasks_cols = {row[1] for row in con.execute("PRAGMA table_info(game_tasks)")}
        users_cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
        coins_cols = {row[1] for row in con.execute("PRAGMA table_info(coins)")}
    finally:
        con.close()
    assert {"wave_id", "audience"} <= game_tasks_cols
    assert {"ambassador_path", "ambassador_since", "ambassador_left_at"} <= users_cols
    assert "task_id" in coins_cols


def test_init_db_idempotent_keeps_users(tmp_path):
    """Двойной init_db() по уже заполненной базе не теряет строк и не падает — прод-база
    Юлида с 2000+ делегатами (T-32-01-04)."""
    _ready(tmp_path)
    _seed_user(1)
    _seed_user(2)
    con = sqlite3.connect(config.DB_PATH)
    try:
        before = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        con.close()
    assert before == 2

    _run(db.init_db())  # второй вызов — не должен упасть и не должен потерять строки

    con = sqlite3.connect(config.DB_PATH)
    try:
        after = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        con.close()
    assert after == before


def test_no_deadline_at_constant():
    assert db.NO_DEADLINE_AT == "9999-12-31 23:59:59"


def test_task_audiences_and_wave_states_constants():
    assert db.TASK_AUDIENCES == ("all", "ambassadors")
    assert db.WAVE_STATES == ("draft", "active", "closing", "announced")
