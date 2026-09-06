"""Quick 260906-8uq (FAQ-01..06): раздел «❓ Частые вопросы» — хранение (`faq_items` +
аксессоры `database/db.py`), чистое правило перекрытия по городу (`services/faq.py`), экран
делегата (бот) и экран менеджера (бот).

pytest-asyncio в проекте нет — каждый async-вызов через `asyncio.run()`; БД — tmp_path,
харнесс `_ready`/`_add_user` в форме `tests/test_sheet_logs_260902.py`.

Задача 1 — блок ниже. Задачи 2/3 дописаны отдельными блоками дальше в этом же файле.
"""
import asyncio

import pytest

from config import config
from database import db
from services import faq as faq_service


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="faq_260906.db"):
    config.DB_PATH = str(tmp_path / name)
    config.GOOGLE_SHEET_ID = ""
    _run(db.init_db())


async def _seed_cities(rows):
    """rows: list of (code, label, tab_base, sort_order[, enabled]) — форма
    tests/test_cities_registry_260818.py::_seed_cities_db. Нужно ТОЛЬКО тем тестам, что
    резолвят `cities.city_scope("kzn")` — чистое правило `services/faq.py` в резолве города
    не нуждается вовсе."""
    import cities
    for r in rows:
        code, label, tab_base, sort_order = r[0], r[1], r[2], r[3]
        enabled = r[4] if len(r) > 4 else 1
        await db.insert_city(code, label, tab_base, sort_order, enabled)
    await cities.reload_cities()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: normalize_question / apply_city_overrides / city_badge / short (services/faq.py)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_normalize_question_collapses_case_and_whitespace_and_trims_trailing_punct():
    a = faq_service.normalize_question("  Где ПРОХОДИТ форум?? ")
    b = faq_service.normalize_question("где проходит форум")
    assert a == b == "где проходит форум"


def test_normalize_question_empty_and_none_are_safe():
    assert faq_service.normalize_question(None) == ""
    assert faq_service.normalize_question("   ") == ""


def test_apply_city_overrides_city_none_keeps_only_general_rows():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "kzn", "question": "Где проходит форум?", "position": 0},
        {"id": 3, "city": "kzn", "question": "Сколько стоит?", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, None)
    assert [r["id"] for r in result] == [1]


def test_apply_city_overrides_same_question_city_wins_over_general():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "kzn", "question": "где проходит форум??", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, "kzn")
    assert [r["id"] for r in result] == [2]


def test_apply_city_overrides_different_question_both_visible():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "kzn", "question": "Что взять с собой?", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, "kzn")
    assert {r["id"] for r in result} == {1, 2}


def test_apply_city_overrides_hides_other_citys_row():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "spb", "question": "Что взять с собой?", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, "kzn")
    assert [r["id"] for r in result] == [1]


def test_apply_city_overrides_sorts_by_position_then_id_on_ties():
    rows = [
        {"id": 5, "city": None, "question": "Б", "position": 0},
        {"id": 3, "city": None, "question": "А", "position": 0},
        {"id": 4, "city": None, "question": "В", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, None)
    assert [r["id"] for r in result] == [3, 5, 4]


def test_city_badge_general_vs_city_label():
    assert faq_service.city_badge(None) == "🌍 все города"
    assert faq_service.city_badge("Казань") == "🏙 Казань"


def test_short_truncates_with_ellipsis():
    assert faq_service.short("Где проходит форум?", 60) == "Где проходит форум?"
    long_text = "А" * 70
    result = faq_service.short(long_text, 10)
    assert result == "А" * 9 + "…"
    assert len(result) == 10


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: аксессоры database/db.py
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_init_db_creates_faq_items_table(tmp_path):
    _ready(tmp_path)

    async def _check():
        async with db._connect() as conn:
            async with conn.execute("PRAGMA table_info(faq_items)") as cursor:
                cols = {row[1] for row in await cursor.fetchall()}
        return cols

    cols = _run(_check())
    assert cols == {
        "id", "city", "question", "answer", "position", "enabled", "created_at", "created_by",
    }


def test_init_db_is_idempotent_and_keeps_existing_faq_rows(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(
        city=None, question="Где проходит форум?", answer="В кампусе.", created_by=1,
    ))
    _run(db.init_db())  # повторный запуск на существующей БД не должен падать/терять строки
    row = _run(db.get_faq_item(item_id))
    assert row is not None
    assert row["question"] == "Где проходит форум?"


def test_create_faq_item_appends_at_end_position(tmp_path):
    _ready(tmp_path)
    first = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    second = _run(db.create_faq_item(city=None, question="B?", answer="b", created_by=1))
    row1 = _run(db.get_faq_item(first))
    row2 = _run(db.get_faq_item(second))
    assert row2["position"] > row1["position"]


def test_reorder_faq_items_writes_sequential_positions(tmp_path):
    _ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    b = _run(db.create_faq_item(city=None, question="B?", answer="b", created_by=1))
    c = _run(db.create_faq_item(city=None, question="C?", answer="c", created_by=1))
    _run(db.reorder_faq_items([c, a, b]))
    rows = {r["id"]: r["position"] for r in _run(db.list_faq_items())}
    assert rows[c] == 0 and rows[a] == 1 and rows[b] == 2


def test_list_faq_items_with_city_scope_includes_city_and_general(tmp_path):
    _ready(tmp_path)
    _run(_seed_cities([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)]))
    import cities
    general = _run(db.create_faq_item(city=None, question="Общий?", answer="o", created_by=1))
    kzn_item = _run(db.create_faq_item(city="kzn", question="Только Казань?", answer="k", created_by=1))
    spb_like = _run(db.create_faq_item(city="msk", question="Только Москва?", answer="m", created_by=1))
    rows = _run(db.list_faq_items(city_scope=cities.city_scope("kzn")))
    ids = {r["id"] for r in rows}
    assert general in ids and kzn_item in ids
    assert spb_like not in ids


def test_has_faq_for_city_false_until_enabled_item_exists(tmp_path):
    _ready(tmp_path)
    assert _run(db.has_faq_for_city("kzn")) is False
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    _run(db.update_faq_item(item_id, enabled=0))
    assert _run(db.has_faq_for_city("kzn")) is False
    _run(db.update_faq_item(item_id, enabled=1))
    assert _run(db.has_faq_for_city("kzn")) is True


def test_list_faq_for_city_returns_general_and_own_city_enabled_only(tmp_path):
    _ready(tmp_path)
    general = _run(db.create_faq_item(city=None, question="Общий?", answer="o", created_by=1))
    kzn_item = _run(db.create_faq_item(city="kzn", question="Казань?", answer="k", created_by=1))
    other = _run(db.create_faq_item(city="spb", question="Питер?", answer="p", created_by=1))
    disabled = _run(db.create_faq_item(city=None, question="Скрыт?", answer="s", created_by=1))
    _run(db.update_faq_item(disabled, enabled=0))
    rows = _run(db.list_faq_for_city("kzn"))
    ids = {r["id"] for r in rows}
    assert ids == {general, kzn_item}


def test_update_faq_item_ignores_keys_outside_whitelist(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    ok = _run(db.update_faq_item(item_id, question="A2?", id=999, created_at="hacked"))
    assert ok is True
    row = _run(db.get_faq_item(item_id))
    assert row["question"] == "A2?"
    assert row["id"] == item_id
    assert row["created_at"] != "hacked"


def test_update_faq_item_with_no_whitelisted_fields_returns_false(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    assert _run(db.update_faq_item(item_id, id=999)) is False


def test_delete_faq_item_removes_row(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    assert _run(db.delete_faq_item(item_id)) is True
    assert _run(db.get_faq_item(item_id)) is None
    assert _run(db.delete_faq_item(item_id)) is False
