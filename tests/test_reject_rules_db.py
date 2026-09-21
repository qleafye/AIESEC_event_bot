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
from database.db import _build_filter_clause


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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: аксессоры журнала автоотказов
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_upsert_auto_reject_log_first_trigger_creates_row_with_attempt_1(tmp_path):
    _ready(tmp_path)
    _seed_user(2001, event_city="msk")
    entry_id = _run(db.upsert_auto_reject_log(
        2001, "[1]", '["текст правила"]', "2026-09-20 10:00:00",
    ))
    row = _run(db.get_auto_reject_log_entry(entry_id))
    assert row["attempt_count"] == 1
    assert row["first_triggered_at"] == "2026-09-20 10:00:00"
    assert row["last_triggered_at"] == "2026-09-20 10:00:00"
    assert row["returned_to_moderation_at"] is None


def test_upsert_auto_reject_log_second_trigger_increments_same_row(tmp_path):
    """Повторное срабатывание того же делегата увеличивает счётчик, а не плодит вторую
    строку (D-18, D-24) — ровно ОДНА строка в auto_reject_log."""
    _ready(tmp_path)
    _seed_user(2002, event_city="msk")
    first_id = _run(db.upsert_auto_reject_log(2002, "[1]", '["a"]', "2026-09-20 10:00:00"))
    second_id = _run(db.upsert_auto_reject_log(2002, "[1, 2]", '["a", "b"]', "2026-09-20 11:00:00"))
    assert first_id == second_id

    row = _run(db.get_auto_reject_log_entry(second_id))
    assert row["attempt_count"] == 2
    assert row["rule_ids"] == "[1, 2]"
    assert row["reject_texts"] == '["a", "b"]'
    assert row["first_triggered_at"] == "2026-09-20 10:00:00"
    assert row["last_triggered_at"] == "2026-09-20 11:00:00"

    async def count_rows():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM auto_reject_log WHERE telegram_id = ?", (2002,)
            ) as cursor:
                r = await cursor.fetchone()
                return r[0]

    assert _run(count_rows()) == 1


def test_claim_auto_reject_return_closes_live_row_and_next_trigger_opens_new_one(tmp_path):
    _ready(tmp_path)
    _seed_user(2003, event_city="msk")
    entry_id = _run(db.upsert_auto_reject_log(2003, "[1]", '["a"]', "2026-09-20 10:00:00"))

    claimed = _run(db.claim_auto_reject_return(entry_id, 777, "2026-09-20 12:00:00"))
    assert claimed is not None
    assert claimed["returned_to_moderation_at"] == "2026-09-20 12:00:00"
    assert claimed["returned_by"] == 777

    # повторный возврат того же id — уже занят, вторая попытка не выигрывает.
    second_attempt = _run(db.claim_auto_reject_return(entry_id, 888, "2026-09-20 13:00:00"))
    assert second_attempt is None

    # следующее срабатывание правила заводит НОВУЮ живую строку (живой больше нет).
    new_id = _run(db.upsert_auto_reject_log(2003, "[1]", '["a"]', "2026-09-20 14:00:00"))
    assert new_id != entry_id
    new_row = _run(db.get_auto_reject_log_entry(new_id))
    assert new_row["attempt_count"] == 1


def test_list_and_count_auto_reject_log_share_the_same_filters(tmp_path):
    """Счётчик и список обязаны ходить по одному набору условий — тот же принцип, что у
    queue_page (иначе «Всего: N» врёт)."""
    _ready(tmp_path)
    _seed_user(2004, event_city="msk")
    _seed_user(2005, event_city="spb")
    _run(db.upsert_auto_reject_log(2004, "[1]", '["a"]', "2026-09-20 10:00:00"))
    _run(db.upsert_auto_reject_log(2005, "[1]", '["a"]', "2026-09-20 10:05:00"))

    msk_rows = _run(db.list_auto_reject_log(city_scope=("msk", ())))
    msk_count = _run(db.count_auto_reject_log(city_scope=("msk", ())))
    assert len(msk_rows) == msk_count == 1
    assert msk_rows[0]["telegram_id"] == 2004
    assert msk_rows[0]["full_name"] == "Delegate 2004"

    all_rows = _run(db.list_auto_reject_log())
    all_count = _run(db.count_auto_reject_log())
    assert len(all_rows) == all_count == 2


def test_list_and_count_auto_reject_log_exclude_returned_by_default(tmp_path):
    _ready(tmp_path)
    _seed_user(2006, event_city="msk")
    entry_id = _run(db.upsert_auto_reject_log(2006, "[1]", '["a"]', "2026-09-20 10:00:00"))
    _run(db.claim_auto_reject_return(entry_id, 1, "2026-09-20 11:00:00"))

    assert _run(db.count_auto_reject_log()) == 0
    assert _run(db.list_auto_reject_log()) == []

    assert _run(db.count_auto_reject_log(include_returned=True)) == 1
    assert len(_run(db.list_auto_reject_log(include_returned=True))) == 1


def test_export_auto_reject_log_rows_passes_values_through_csv_safe(tmp_path):
    """ФИО делегата, начинающееся с `=` (анкета — свободный текст, ничем не ограничена),
    обязано пройти `_csv_safe` (T-31-02-02, CWE-1236) в выгрузке журнала."""
    _ready(tmp_path)
    _run(db.add_user({
        "telegram_id": 2007,
        "full_name": "=HYPERLINK(\"evil\")",
        "registration_date": "2026-01-01 00:00:07",
        "event_city": "msk",
    }))
    _run(db.upsert_auto_reject_log(2007, "[1]", '["a"]', "2026-09-20 10:00:00"))
    headers, rows = _run(db.export_auto_reject_log_rows())
    assert len(headers) == len(rows[0])
    full_name_cell = rows[0][headers.index("ФИО")]
    assert isinstance(full_name_cell, str)
    assert full_name_cell.startswith("'=")


def test_resolve_decision_managers_skips_auto_reject_sentinel(tmp_path):
    """`resolve_decision_managers([-1, 12345])` не содержит ключа `-1` — сентинел автоотказа
    не должен уезжать в запрос имён менеджеров (план 31-05, services/reject_journal.py)."""
    _ready(tmp_path)
    labels = _run(db.resolve_decision_managers([-1, 12345]))
    assert -1 not in labels
    assert 12345 in labels


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: три шва — flagged_only, auto_reject фильтр рассылки, auto_rejected в дайджесте
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_flagged_only_true_returns_only_flagged_pending(tmp_path):
    _ready(tmp_path)
    _seed_user(3001)
    _seed_user(3002)
    _run(db.set_user_status(3001, "pending"))
    _run(db.set_user_status(3002, "pending"))
    _run(_set_user_field(3001, "flagged_rule_ids", "[7]"))

    flagged = _run(db.get_pending_users(limit=10, flagged_only=True))
    assert [r["telegram_id"] for r in flagged] == [3001]
    assert _run(db.get_pending_count(flagged_only=True)) == 1


def test_flagged_only_false_keeps_previous_behaviour(tmp_path):
    _ready(tmp_path)
    _seed_user(3003)
    _seed_user(3004)
    _run(db.set_user_status(3003, "pending"))
    _run(db.set_user_status(3004, "pending"))
    _run(_set_user_field(3003, "flagged_rule_ids", "[7]"))

    rows = _run(db.get_pending_users(limit=10))
    assert {r["telegram_id"] for r in rows} == {3003, 3004}
    assert _run(db.get_pending_count()) == 2


def test_flagged_only_treats_empty_list_as_not_flagged(tmp_path):
    _ready(tmp_path)
    _seed_user(3005)
    _run(db.set_user_status(3005, "pending"))
    _run(_set_user_field(3005, "flagged_rule_ids", "[]"))

    assert _run(db.get_pending_count(flagged_only=True)) == 0


def test_auto_reject_is_registered_in_filter_columns_and_virtual_fields():
    assert "auto_reject" in db._FILTER_COLUMNS
    assert "auto_reject" in db._FILTER_VIRTUAL_FIELDS


def test_filter_columns_whitelist_guard(tmp_path):
    """Каждое поле `_FILTER_COLUMNS` — либо реальная колонка `users`, либо объявлено
    виртуальным в `_FILTER_VIRTUAL_FIELDS` (тот же guard, что
    tests/test_broadcast_resume_filter_260911.py — ловит опечатку в имени колонки)."""
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        real_cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    finally:
        con.close()
    for field in db._FILTER_COLUMNS:
        assert field in real_cols or field in db._FILTER_VIRTUAL_FIELDS, (
            f"'{field}' is neither a real users column nor declared virtual"
        )


def test_build_filter_clause_auto_reject_yes_and_no():
    where_yes, params_yes = _build_filter_clause([{"field": "auto_reject", "value": db.AUTO_REJECT_YES}])
    assert params_yes == []
    assert "auto_reject_rule_ids" in where_yes

    where_no, params_no = _build_filter_clause([{"field": "auto_reject", "value": db.AUTO_REJECT_NO}])
    assert params_no == []
    assert "auto_reject_rule_ids" in where_no
    assert where_yes != where_no


def test_build_filter_clause_auto_reject_garbage_value_is_fail_closed():
    assert _build_filter_clause([{"field": "auto_reject", "value": "garbage"}]) == (" WHERE 0", [])


def test_build_filter_clause_auto_reject_empty_value_is_fail_closed():
    assert _build_filter_clause([{"field": "auto_reject", "value": ""}]) == (" WHERE 0", [])


def test_count_and_list_filtered_auto_reject_yes_and_no_partition_base(tmp_path):
    _ready(tmp_path)
    _seed_user(3101)
    _seed_user(3102)
    _run(_set_user_field(3101, "auto_reject_rule_ids", "[1]"))

    yes_ids = _run(db.count_and_list_filtered([{"field": "auto_reject", "value": db.AUTO_REJECT_YES}]))
    no_ids = _run(db.count_and_list_filtered([{"field": "auto_reject", "value": db.AUTO_REJECT_NO}]))
    assert set(yes_ids) == {3101}
    assert set(no_ids) == {3102}


def test_get_distinct_filter_values_auto_reject_returns_empty_not_crash(tmp_path):
    """Виртуальное поле — тот же прецедент, что resume: `SELECT DISTINCT auto_reject` упал бы
    `OperationalError` без ветки `_FILTER_VIRTUAL_FIELDS`."""
    _ready(tmp_path)
    assert _run(db.get_distinct_filter_values("auto_reject")) == []


def test_get_auto_reject_filter_options_both_sides(tmp_path):
    _ready(tmp_path)
    _seed_user(3103)
    _seed_user(3104)
    _run(_set_user_field(3103, "auto_reject_rule_ids", "[1]"))

    options = _run(db.get_auto_reject_filter_options())
    assert options == [db.AUTO_REJECT_YES, db.AUTO_REJECT_NO]


def test_get_auto_reject_filter_options_empty_base(tmp_path):
    _ready(tmp_path)
    assert _run(db.get_auto_reject_filter_options()) == []


def test_enqueue_reg_digest_accepts_auto_rejected_kwarg(tmp_path):
    _ready(tmp_path)
    _seed_user(3201)
    qid = _run(db.enqueue_reg_digest(3201, "msk", "2026-09-20 10:00:00", auto_rejected=1))
    rows = _run(db.list_unsent_reg_digest("msk"))
    row = next(r for r in rows if r["id"] == qid)
    assert row["auto_rejected"] == 1


def test_enqueue_reg_digest_default_auto_rejected_is_zero(tmp_path):
    """Хвостовой kwarg с дефолтом — существующие вызывающие без нового аргумента остаются
    байт-в-байт прежними."""
    _ready(tmp_path)
    _seed_user(3202)
    qid = _run(db.enqueue_reg_digest(3202, "msk", "2026-09-20 10:00:00"))
    rows = _run(db.list_unsent_reg_digest("msk"))
    row = next(r for r in rows if r["id"] == qid)
    assert row["auto_rejected"] == 0


def test_reg_submit_digest_queue_has_auto_rejected_column(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(reg_submit_digest_queue)")}
    finally:
        con.close()
    assert "auto_rejected" in cols
