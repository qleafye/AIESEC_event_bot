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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: аксессоры волн, заданий, амбассадоров
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_numbering_per_city(tmp_path):
    _ready(tmp_path)
    w1 = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00", event_city="msk"))
    w2 = _run(db.create_wave("2026-10-09 00:00:00", "2026-10-16 00:00:00", event_city="msk"))
    w3 = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00", event_city="spb"))
    assert _run(db.get_wave(w1))["number"] == 1
    assert _run(db.get_wave(w2))["number"] == 2
    assert _run(db.get_wave(w3))["number"] == 1


def test_create_task_old_call_stays_outside_waves_and_all_audience(tmp_path):
    """Старый вызов create_task без новых kwargs продолжает создавать задание вне волн и
    видимое всем — старые call sites не ломаются."""
    _ready(tmp_path)
    task_id = _run(db.create_task(
        "Сделай штуку", "Light", 10, "photo", "2026-10-01 00:00:00", None,
    ))
    task = _run(db.get_task(task_id))
    assert task["wave_id"] is None
    assert task["audience"] == "all"


def test_update_wave_rejects_unknown_field(tmp_path):
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    with pytest.raises(ValueError):
        _run(db.update_wave(wave_id, state="active"))


def test_update_wave_known_field(tmp_path):
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    assert _run(db.update_wave(wave_id, intro_text="Привет!")) is True
    assert _run(db.get_wave(wave_id))["intro_text"] == "Привет!"


def test_set_wave_state_expected_state_wins_once(tmp_path):
    """Два одновременных перехода с expected_state выигрывает ровно один — второй возвращает
    False, потому что state больше не 'active'."""
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    first = _run(db.set_wave_state(wave_id, "closing", expected_state="active"))
    second = _run(db.set_wave_state(wave_id, "closing", expected_state="active"))
    assert first is True
    assert second is False


def test_delete_wave_clears_own_tasks_not_others(tmp_path):
    _ready(tmp_path)
    wave_a = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    wave_b = _run(db.create_wave("2026-11-01 00:00:00", "2026-11-08 00:00:00"))
    task_a = _run(db.create_task(
        "A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_a,
    ))
    task_b = _run(db.create_task(
        "B", "Light", 10, "photo", "2026-11-05 00:00:00", None, wave_id=wave_b,
    ))
    assert _run(db.delete_wave(wave_a)) is True
    assert _run(db.get_task(task_a))["wave_id"] is None
    assert _run(db.get_task(task_b))["wave_id"] == wave_b
    assert _run(db.get_wave(wave_a)) is None


def test_waves_overlapping_catches_edge_touch_not_adjacent(tmp_path):
    _ready(tmp_path)
    base = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    # касание краем (новая волна начинается в момент конца существующей) — пересечение
    touching = _run(db.waves_overlapping(
        "2026-10-08 00:00:00", "2026-10-15 00:00:00", None, exclude_id=None,
    ))
    assert any(row["id"] == base for row in touching)
    # соседний отрезок без касания — не пересечение
    adjacent = _run(db.waves_overlapping(
        "2026-10-09 00:00:00", "2026-10-16 00:00:00", None, exclude_id=None,
    ))
    assert not any(row["id"] == base for row in adjacent)


def test_wave_at_returns_none_for_draft(tmp_path):
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    # ещё черновик — wave_at ничего не находит
    assert _run(db.wave_at("2026-10-03 00:00:00", None)) is None
    _run(db.set_wave_state(wave_id, "active"))
    found = _run(db.wave_at("2026-10-03 00:00:00", None))
    assert found is not None
    assert found["id"] == wave_id


def test_set_ambassador_flag_active_false_keeps_since(tmp_path):
    _ready(tmp_path)
    _seed_user(1)
    _run(db.set_ambassador_flag(1, active=True, at="2026-09-01 00:00:00"))
    _run(db.set_ambassador_flag(1, active=False, at="2026-09-20 00:00:00"))
    user = _run(db.get_user(1))
    assert user["is_ambassador"] == 0
    assert user["ambassador_since"] == "2026-09-01 00:00:00"
    assert user["ambassador_left_at"] == "2026-09-20 00:00:00"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: аксессоры начислений и сторож пожизненного рейтинга
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_claim_referral_credit_idempotent(tmp_path):
    """Двойной claim_referral_credit для одного приглашённого — True, потом False, в
    таблице ровно одна строка (T-32-01-01)."""
    _ready(tmp_path)
    _seed_user(1)
    _seed_user(2)
    first = _run(db.claim_referral_credit(2, 1, 50, None))
    second = _run(db.claim_referral_credit(2, 1, 50, None))
    assert first is True
    assert second is False
    rows = _run(db.list_referral_credits(referrer_id=1))
    assert len(rows) == 1
    assert _run(db.count_referral_credits(1, None)) == 1


def test_insert_wave_results_immutable(tmp_path):
    """Повторный insert_wave_results — 0 вставок, содержимое снимка не меняется (D-17)."""
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    first = _run(db.insert_wave_results(
        wave_id, [(1, 1, 100), (2, 2, 80)], "2026-10-09 00:00:00",
    ))
    second = _run(db.insert_wave_results(
        wave_id, [(1, 1, 999), (2, 2, 999)], "2026-10-10 00:00:00",
    ))
    assert first == 2
    assert second == 0
    results = _run(db.get_wave_results(wave_id))
    assert [r["points"] for r in results] == [100, 80]


def test_sum_task_coins_for_wave_scoped_and_ignores_legacy(tmp_path):
    """Делегат с двумя заданиями ОДНОЙ волны и одним заданием ДРУГОЙ волны получает по
    sum_task_coins_for_wave ровно суммы своей волны; строка coins без task_id (легаси) в
    суммы волны не попадает (Pitfall 4)."""
    _ready(tmp_path)
    _seed_user(1)
    wave_a = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    wave_b = _run(db.create_wave("2026-11-01 00:00:00", "2026-11-08 00:00:00"))
    task_a1 = _run(db.create_task(
        "A1", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_a,
    ))
    task_a2 = _run(db.create_task(
        "A2", "Light", 20, "photo", "2026-10-06 00:00:00", None, wave_id=wave_a,
    ))
    task_b1 = _run(db.create_task(
        "B1", "Light", 30, "photo", "2026-11-05 00:00:00", None, wave_id=wave_b,
    ))
    _run(db.add_coins(1, 10, source="task", task_id=task_a1))
    _run(db.add_coins(1, 20, source="task", task_id=task_a2))
    _run(db.add_coins(1, 30, source="task", task_id=task_b1))
    _run(db.add_coins(1, 999, source="task"))  # легаси-строка без task_id — не в счёт волны
    sums_a = _run(db.sum_task_coins_for_wave(wave_a))
    sums_b = _run(db.sum_task_coins_for_wave(wave_b))
    assert sums_a[1] == 30
    assert sums_b[1] == 30


def test_lifetime_leaderboard_unchanged_by_new_coin_sources(tmp_path):
    """Сторож пожизненного рейтинга (D-15): на наборе ручных, задачных и реферальных
    начислений get_leaderboard/get_user_rank дают те же числа, что прямая сумма SUM(delta)
    по ВСЕМ строкам журнала — в общий зачёт идёт ВСЁ, новая колонка task_id функции не меняет."""
    _ready(tmp_path)
    _seed_user(1)
    _seed_user(2)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_id = _run(db.create_task(
        "A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id,
    ))
    _run(db.add_coins(1, 50, source="manual"))
    _run(db.add_coins(1, 10, source="task", task_id=task_id))
    _run(db.add_coins(2, 100))  # легаси, source=None, task_id=None
    _run(db.add_coins(2, 25, source="referral"))  # реферальное начисление в общем журнале

    leaderboard = _run(db.get_leaderboard(10))
    rank_1 = _run(db.get_user_rank(1))
    rank_2 = _run(db.get_user_rank(2))

    con = sqlite3.connect(config.DB_PATH)
    try:
        direct = dict(con.execute(
            "SELECT user_id, SUM(delta) FROM coins GROUP BY user_id"
        ).fetchall())
    finally:
        con.close()

    lb_by_user = {row["user_id"]: row["balance"] for row in leaderboard}
    assert lb_by_user == direct

    ordered = sorted(direct.items(), key=lambda kv: -kv[1])
    expected_rank = {uid: i + 1 for i, (uid, _) in enumerate(ordered)}
    assert rank_1 == expected_rank[1]
    assert rank_2 == expected_rank[2]
