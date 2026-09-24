"""Форум-ночь п.4 (расписание форума в боте) — БД-слой: `program_halls`/`program_sessions` и
CRUD `database.db.create_program_hall`/`list_program_halls`/`rename_program_hall`/
`delete_program_hall`/`create_program_session`/`update_program_session`/
`delete_program_session`/`list_program_sessions_for_city_day`/`list_program_days_for_city`/
`has_program_sessions_for_city`/`sessions_overlapping_hall`.

БД — шаблонная копия через `tests/_dbtpl.py::fast_init_db` (см. докстринг модуля) вместо
полного прогона `init_db()`; `asyncio.run()` — pytest-asyncio недоступен в этом окружении, тот
же приём, что у соседних тестов `test_checkin_db_260924.py`.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from tests._dbtpl import fast_init_db


def _use_tmp_db(tmp_path, name="test_program_db.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


# ── program_halls ────────────────────────────────────────────────────────────────────────────

def test_create_and_list_program_halls_ordered_by_sort(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_hall("msk", "Большой зал"))
    asyncio.run(db.create_program_hall("msk", "Малый зал", capacity=30))
    asyncio.run(db.create_program_hall("spb", "Зал СПб"))  # другой город -- не должен попасть

    halls = asyncio.run(db.list_program_halls("msk"))
    assert [h["name"] for h in halls] == ["Большой зал", "Малый зал"]
    assert halls[0]["sort_order"] == 0
    assert halls[1]["sort_order"] == 1
    assert halls[1]["capacity"] == 30
    assert halls[0]["capacity"] is None


def test_get_program_hall_roundtrip_and_missing(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    found = asyncio.run(db.get_program_hall(hall_id))
    assert found is not None
    assert found["name"] == "Большой зал"
    assert asyncio.run(db.get_program_hall(hall_id + 999)) is None


def test_rename_program_hall(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Старое имя"))
    assert asyncio.run(db.rename_program_hall(hall_id, "Новое имя")) is True
    assert asyncio.run(db.get_program_hall(hall_id))["name"] == "Новое имя"
    assert asyncio.run(db.rename_program_hall(hall_id + 999, "X")) is False


def test_delete_program_hall_orphans_sessions_not_deletes_them(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    session_id = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Открытие", hall_id=hall_id,
    ))
    assert asyncio.run(db.count_program_sessions_for_hall(hall_id)) == 1
    assert asyncio.run(db.delete_program_hall(hall_id)) is True
    assert asyncio.run(db.get_program_hall(hall_id)) is None
    session = asyncio.run(db.get_program_session(session_id))
    assert session is not None  # сессия осталась
    assert session["hall_id"] is None  # но потеряла привязку к залу


def test_delete_program_hall_missing_returns_false(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(db.delete_program_hall(12345)) is False


# ── program_sessions CRUD ────────────────────────────────────────────────────────────────────

def test_create_and_get_program_session(tmp_path):
    _use_tmp_db(tmp_path)
    session_id = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:30", "Открытие форума",
        speaker="Иван Иванов", description="Вступительное слово",
    ))
    session = asyncio.run(db.get_program_session(session_id))
    assert session["city"] == "msk"
    assert session["day"] == "2026-10-30"
    assert session["start_time"] == "10:00"
    assert session["end_time"] == "11:30"
    assert session["title"] == "Открытие форума"
    assert session["speaker"] == "Иван Иванов"
    assert session["description"] == "Вступительное слово"
    assert session["hall_id"] is None
    assert session["created_at"]
    assert session["updated_at"]


def test_get_program_session_missing_returns_none(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(db.get_program_session(999)) is None


def test_update_program_session_partial_patch(tmp_path):
    _use_tmp_db(tmp_path)
    session_id = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Название",
    ))
    old = asyncio.run(db.get_program_session(session_id))
    assert asyncio.run(db.update_program_session(session_id, title="Новое название")) is True
    updated = asyncio.run(db.get_program_session(session_id))
    assert updated["title"] == "Новое название"
    assert updated["start_time"] == "10:00"  # не тронуто
    assert updated["updated_at"] >= old["updated_at"]


def test_update_program_session_unknown_fields_only_returns_false(tmp_path):
    _use_tmp_db(tmp_path)
    session_id = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Название",
    ))
    assert asyncio.run(db.update_program_session(session_id, bogus_field="x")) is False


def test_update_program_session_missing_returns_false(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(db.update_program_session(999, title="X")) is False


def test_delete_program_session(tmp_path):
    _use_tmp_db(tmp_path)
    session_id = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Название",
    ))
    assert asyncio.run(db.delete_program_session(session_id)) is True
    assert asyncio.run(db.get_program_session(session_id)) is None
    assert asyncio.run(db.delete_program_session(session_id)) is False


# ── city/day listings ────────────────────────────────────────────────────────────────────────

def test_list_program_sessions_for_city_day_ordered_by_time_and_city_isolated(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "Обед"))
    asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    asyncio.run(db.create_program_session("msk", "2026-10-31", "09:00", "10:00", "Второй день"))
    asyncio.run(db.create_program_session("spb", "2026-10-30", "10:00", "11:00", "Открытие СПб"))

    sessions = asyncio.run(db.list_program_sessions_for_city_day("msk", "2026-10-30"))
    assert [s["title"] for s in sessions] == ["Открытие", "Обед"]


def test_list_program_days_for_city_distinct_and_sorted(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "A"))
    asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "B"))
    asyncio.run(db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "C"))
    days = asyncio.run(db.list_program_days_for_city("msk"))
    assert days == ["2026-10-30", "2026-10-31"]


def test_has_program_sessions_for_city(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(db.has_program_sessions_for_city("msk")) is False
    asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "A"))
    assert asyncio.run(db.has_program_sessions_for_city("msk")) is True
    assert asyncio.run(db.has_program_sessions_for_city("spb")) is False


# ── overlap detection ────────────────────────────────────────────────────────────────────────

def test_sessions_overlapping_hall_detects_overlap(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Первая", hall_id=hall_id,
    ))
    overlaps = asyncio.run(db.sessions_overlapping_hall(
        "msk", "2026-10-30", hall_id, "10:30", "11:30",
    ))
    assert len(overlaps) == 1
    assert overlaps[0]["title"] == "Первая"


def test_sessions_overlapping_hall_no_overlap_when_adjacent(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Первая", hall_id=hall_id,
    ))
    # Соприкасаются, но не пересекаются -- [10:00,11:00) и [11:00,12:00) не конфликтуют.
    overlaps = asyncio.run(db.sessions_overlapping_hall(
        "msk", "2026-10-30", hall_id, "11:00", "12:00",
    ))
    assert overlaps == []


def test_sessions_overlapping_hall_different_hall_no_conflict(tmp_path):
    _use_tmp_db(tmp_path)
    hall_a = asyncio.run(db.create_program_hall("msk", "Зал А"))
    hall_b = asyncio.run(db.create_program_hall("msk", "Зал Б"))
    asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Первая", hall_id=hall_a,
    ))
    overlaps = asyncio.run(db.sessions_overlapping_hall(
        "msk", "2026-10-30", hall_b, "10:00", "11:00",
    ))
    assert overlaps == []


def test_sessions_overlapping_hall_excludes_self_on_edit(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    session_id = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Первая", hall_id=hall_id,
    ))
    # Правка САМОЙ этой сессии (то же время) не должна конфликтовать сама с собой.
    overlaps = asyncio.run(db.sessions_overlapping_hall(
        "msk", "2026-10-30", hall_id, "10:00", "11:00", exclude_id=session_id,
    ))
    assert overlaps == []
