"""Phase 31 Plan 02 (D-05/D-15/D-18/D-20/D-24/D-26/D-28): вся работа с базой под автоотказ —
две новые таблицы, четыре новые колонки `users`, аксессоры правил и журнала, три SQL-шва в
существующих запросах (очередь заявок, фильтр рассылки, очередь дайджеста).

Три пласта сторожей — по одному на задачу плана:
- Задача 1: схема (`reject_rules`, `auto_reject_log`, колонки `users`) + CRUD правил.
- Задача 2: аксессоры журнала автоотказов (счётчик попыток, возврат на модерацию, выгрузка).
- Задача 3: три шва — `flagged_only` в очереди заявок, `auto_reject` в фильтре рассылки,
  `auto_rejected` в очереди дайджеста заявок.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_skillup_scoring_28.py::_ready(tmp_path)` /
`tests/test_applications_db.py::_use_tmp_db`.
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db


def _ready(tmp_path, name="test_reject_rules_db.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, registration_date=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": registration_date or f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
    }))


async def _set_user_field(tid, field, value):
    async with db._connect() as conn:
        await conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, tid))
        await conn.commit()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: схема — две таблицы, четыре колонки users, аксессоры правил
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_init_db_is_idempotent(tmp_path):
    """Двойной вызов init_db() на уже мигрированной базе не падает (T-31-02-03) — прод-база
    (2000+ строк) должна пройти миграцию молча."""
    _ready(tmp_path)
    _run(db.init_db())  # второй вызов — не должен упасть


def test_reject_rules_table_has_all_columns(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(reject_rules)")}
    finally:
        con.close()
    expected = {
        "id", "name", "city", "tracks", "conditions", "action", "reject_text",
        "enabled", "paused_reason", "created_at", "updated_at", "created_by",
    }
    assert expected <= cols


def test_auto_reject_log_table_has_all_columns(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(auto_reject_log)")}
    finally:
        con.close()
    expected = {
        "id", "telegram_id", "rule_ids", "reject_texts", "attempt_count",
        "first_triggered_at", "last_triggered_at", "returned_to_moderation_at", "returned_by",
    }
    assert expected <= cols


def test_users_table_has_four_new_auto_reject_columns(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    finally:
        con.close()
    for col in ("auto_reject_rule_ids", "auto_rejected_at", "flagged_rule_ids", "auto_rule_note"):
        assert col in cols


def test_auto_reject_log_live_index_is_partial_unique(tmp_path):
    """Частичный уникальный индекс — «живая» (`returned_to_moderation_at IS NULL`) строка
    у делегата ровно одна: вставка второй живой строки для того же telegram_id обязана
    упасть на уровне SQLite."""
    _ready(tmp_path)

    async def go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO auto_reject_log (telegram_id, rule_ids, reject_texts, "
                "first_triggered_at, last_triggered_at) VALUES (?, ?, ?, ?, ?)",
                (1, "[1]", '["текст"]', "2026-09-20 10:00:00", "2026-09-20 10:00:00"),
            )
            await conn.commit()
            raised = False
            try:
                await conn.execute(
                    "INSERT INTO auto_reject_log (telegram_id, rule_ids, reject_texts, "
                    "first_triggered_at, last_triggered_at) VALUES (?, ?, ?, ?, ?)",
                    (1, "[2]", '["текст2"]', "2026-09-20 11:00:00", "2026-09-20 11:00:00"),
                )
                await conn.commit()
            except sqlite3.IntegrityError:
                raised = True
            except Exception as exc:  # aiosqlite оборачивает sqlite3.IntegrityError
                raised = "UNIQUE" in str(exc) or "IntegrityError" in type(exc).__name__
            return raised

    assert _run(go()) is True


def test_reject_rule_crud_roundtrip(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_reject_rule(
        name="Москва 1-2 курс", city="msk", tracks='["full"]',
        conditions='[[{"field": "course", "op": "in", "values": ["1", "2"]}]]',
        action="reject", reject_text="Места на 1-2 курс закончились.",
        enabled=0, created_by=555,
    ))
    assert rid > 0

    row = _run(db.get_reject_rule(rid))
    assert row["name"] == "Москва 1-2 курс"
    assert row["city"] == "msk"
    assert row["action"] == "reject"
    assert row["enabled"] == 0
    assert row["created_at"] == row["updated_at"]

    ok = _run(db.update_reject_rule(rid, enabled=1, reject_text="Новый текст"))
    assert ok is True
    row2 = _run(db.get_reject_rule(rid))
    assert row2["enabled"] == 1
    assert row2["reject_text"] == "Новый текст"
    assert row2["updated_at"] >= row["updated_at"]

    assert _run(db.count_reject_rules()) == 1
    assert _run(db.count_reject_rules(enabled_only=True)) == 1

    deleted = _run(db.delete_reject_rule(rid))
    assert deleted is True
    assert _run(db.get_reject_rule(rid)) is None
    assert _run(db.count_reject_rules()) == 0


def test_reject_rule_all_cities_visible_from_any_city_scope(tmp_path):
    """Правило city=NULL («все города») обязано быть видно из ЛЮБОГО городского скоупа —
    та же семантика, что у admin_faq._card_out_of_scope."""
    _ready(tmp_path)
    all_cities_id = _run(db.create_reject_rule(
        name="Общее", city=None, tracks='["full"]', conditions="[]",
        action="flag", reject_text=None, enabled=1, created_by=None,
    ))
    msk_id = _run(db.create_reject_rule(
        name="Москва", city="msk", tracks='["full"]', conditions="[]",
        action="flag", reject_text=None, enabled=1, created_by=None,
    ))

    spb_scope_rows = _run(db.list_reject_rules(city_scope=("spb", ())))
    ids = {r["id"] for r in spb_scope_rows}
    assert all_cities_id in ids
    assert msk_id not in ids

    msk_scope_rows = _run(db.list_reject_rules(city_scope=("msk", ())))
    ids2 = {r["id"] for r in msk_scope_rows}
    assert all_cities_id in ids2
    assert msk_id in ids2


def test_update_reject_rule_ignores_columns_outside_whitelist(tmp_path):
    """T-31-02-01: `update_reject_rule(rid, telegram_id=1)` не меняет ничего и не падает —
    имя колонки никогда не приходит из вызывающего в сыром виде."""
    _ready(tmp_path)
    rid = _run(db.create_reject_rule(
        name="Тест", city=None, tracks='["full"]', conditions="[]",
        action="reject", reject_text="текст", enabled=0, created_by=None,
    ))
    before = _run(db.get_reject_rule(rid))
    ok = _run(db.update_reject_rule(rid, telegram_id=1))
    assert ok is False
    after = _run(db.get_reject_rule(rid))
    assert after == before


def test_get_reject_rule_missing_returns_none(tmp_path):
    _ready(tmp_path)
    assert _run(db.get_reject_rule(999999)) is None


def test_delete_reject_rule_missing_returns_false(tmp_path):
    _ready(tmp_path)
    assert _run(db.delete_reject_rule(999999)) is False
