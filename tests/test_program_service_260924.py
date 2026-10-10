"""Форум-ночь п.4 (расписание форума в боте) — сервисный слой `services/forum/program.py`: разбор
времени/дня, предупреждение о занятости зала, слоты параллельных сессий, копирование программы
между городами. БД — шаблонная копия через `tests/_dbtpl.py::fast_init_db`, `asyncio.run()` —
pytest-asyncio недоступен в этом окружении (тот же приём, что у соседних тестов БД-слоя).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from config import config
from database import db
from services.forum import program
from tests._dbtpl import fast_init_db


def _use_tmp_db(tmp_path, name="test_program_service.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


# ── parse_time_range: варианты ввода из подсказки + ошибки ──────────────────────────────────

@pytest.mark.parametrize("raw", [
    "10:00-11:30",
    "10:00 - 11:30",
    "10.00 11.30",
    "10–11:30",
    "10:00–11:30",
])
def test_parse_time_range_accepts_all_hint_variants(raw):
    assert program.parse_time_range(raw) == ("10:00", "11:30")


@pytest.mark.parametrize("raw", [
    "",
    None,
    "не время",
    "10:00",  # только одно время
    "10:00-11:30-12:00",  # три числа
    "25:00-26:00",  # вне диапазона часов
    "10:70-11:30",  # вне диапазона минут
])
def test_parse_time_range_rejects_garbage(raw):
    assert program.parse_time_range(raw) is None


def test_parse_time_range_rejects_end_before_or_equal_start():
    assert program.parse_time_range("11:00-10:00") is None
    assert program.parse_time_range("10:00-10:00") is None


def test_format_time_range_uses_en_dash():
    assert program.format_time_range("10:00", "11:30") == "10:00–11:30"


# ── parse_day_input / day_label / suggested_days ─────────────────────────────────────────────

def test_parse_day_input_full_date():
    assert program.parse_day_input("31.10.2026") == "2026-10-31"


def test_parse_day_input_iso_passthrough():
    assert program.parse_day_input("2026-10-31") == "2026-10-31"


def test_parse_day_input_day_month_uses_reference_year():
    assert program.parse_day_input("31.10", reference=date(2026, 1, 1)) == "2026-10-31"


def test_parse_day_input_garbage_returns_none():
    assert program.parse_day_input("не дата") is None
    assert program.parse_day_input("") is None
    assert program.parse_day_input("31.13") is None


def test_day_label_formats_iso_to_human():
    assert program.day_label("2026-10-31") == "31.10.2026"


def test_day_label_passthrough_on_bad_input():
    assert program.day_label("bogus") == "bogus"


def test_suggested_days_none_forum_date_returns_empty():
    assert program.suggested_days(None) == []


def test_suggested_days_returns_three_consecutive_days():
    forum_dt = datetime(2026, 10, 30)
    assert program.suggested_days(forum_dt) == ["2026-10-30", "2026-10-31", "2026-11-01"]


# ── hall_conflict_warning ─────────────────────────────────────────────────────────────────────

def test_hall_conflict_warning_none_without_hall(tmp_path):
    _use_tmp_db(tmp_path)
    warning = asyncio.run(program.hall_conflict_warning("msk", "2026-10-30", None, "10:00", "11:00"))
    assert warning is None


def test_hall_conflict_warning_none_when_no_overlap(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    warning = asyncio.run(
        program.hall_conflict_warning("msk", "2026-10-30", hall_id, "10:00", "11:00")
    )
    assert warning is None


def test_hall_conflict_warning_names_hall_and_other_session(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Открытие", hall_id=hall_id,
    ))
    warning = asyncio.run(
        program.hall_conflict_warning("msk", "2026-10-30", hall_id, "10:30", "11:30")
    )
    assert warning is not None
    assert "Большой зал" in warning
    assert "Открытие" in warning
    assert "10:00–11:00" in warning


# ── sessions_for_city_day (join hall_name) ───────────────────────────────────────────────────

def test_sessions_for_city_day_joins_hall_name(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал"))
    asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Открытие", hall_id=hall_id,
    ))
    asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "11:00", "12:00", "Без зала",
    ))
    sessions = asyncio.run(program.sessions_for_city_day("msk", "2026-10-30"))
    by_title = {s["title"]: s for s in sessions}
    assert by_title["Открытие"]["hall_name"] == "Большой зал"
    assert by_title["Без зала"]["hall_name"] is None


# ── sessions_now ──────────────────────────────────────────────────────────────────────────────

def test_sessions_now_returns_current_session_only(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Утро"))
    asyncio.run(db.create_program_session("msk", "2026-10-30", "11:00", "12:00", "День"))
    now = asyncio.run(program.sessions_now("msk", datetime(2026, 10, 30, 10, 30)))
    assert [s["title"] for s in now] == ["Утро"]


def test_sessions_now_boundary_end_exclusive(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Утро"))
    now = asyncio.run(program.sessions_now("msk", datetime(2026, 10, 30, 11, 0)))
    assert now == []


def test_sessions_now_empty_when_nothing_running(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Утро"))
    now = asyncio.run(program.sessions_now("msk", datetime(2026, 10, 30, 9, 0)))
    assert now == []


# ── group_parallel / parallel_group ──────────────────────────────────────────────────────────

def test_group_parallel_transitive_overlap_forms_one_slot():
    sessions = [
        {"id": 1, "start_time": "10:00", "end_time": "11:00"},
        {"id": 2, "start_time": "10:30", "end_time": "11:30"},
        {"id": 3, "start_time": "11:15", "end_time": "12:00"},  # пересекается только с #2
    ]
    groups = program.group_parallel(sessions)
    assert len(groups) == 1
    assert {s["id"] for s in groups[0]} == {1, 2, 3}


def test_group_parallel_separate_slots_when_no_overlap():
    sessions = [
        {"id": 1, "start_time": "10:00", "end_time": "11:00"},
        {"id": 2, "start_time": "11:00", "end_time": "12:00"},  # соприкасается, не пересекается
    ]
    groups = program.group_parallel(sessions)
    assert len(groups) == 2


def test_parallel_group_returns_own_slot():
    sessions = [
        {"id": 1, "start_time": "10:00", "end_time": "11:00"},
        {"id": 2, "start_time": "10:30", "end_time": "11:30"},
        {"id": 3, "start_time": "14:00", "end_time": "15:00"},
    ]
    group = program.parallel_group(sessions[0], sessions)
    assert {s["id"] for s in group} == {1, 2}


def test_parallel_group_alone_when_no_overlap():
    sessions = [{"id": 1, "start_time": "10:00", "end_time": "11:00"}]
    group = program.parallel_group(sessions[0], sessions)
    assert [s["id"] for s in group] == [1]


# ── point_for_session ─────────────────────────────────────────────────────────────────────────

def test_point_for_session_format():
    assert program.point_for_session(42) == "session:42"


# ── session_point_label / checkin_session_points (форум-ночь п.5, D-18) ─────────────────────

def test_session_point_label_with_hall_and_truncated_title():
    session = {
        "start_time": "10:00", "end_time": "11:00", "hall_name": "Большой зал",
        "title": "Очень длинное название сессии, которое точно длиннее лимита в сорок символов",
    }
    label = program.session_point_label(session, limit=20)
    assert label.startswith("10:00–11:00 · Большой зал · ")
    assert label.endswith("…")
    assert len(label.split(" · ")[-1]) == 20


def test_session_point_label_without_hall():
    session = {"start_time": "10:00", "end_time": "11:00", "hall_name": None, "title": "Открытие"}
    assert program.session_point_label(session) == "10:00–11:00 · Открытие"


def test_checkin_session_points_empty_when_no_sessions_today(tmp_path):
    _use_tmp_db(tmp_path)
    points = asyncio.run(program.checkin_session_points("msk", datetime(2026, 10, 30, 10, 0)))
    assert points == []


def test_checkin_session_points_live_first_then_by_start_time(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session("msk", "2026-10-30", "09:00", "10:00", "Утро"))
    id_now = asyncio.run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Идёт сейчас"))
    asyncio.run(db.create_program_session("msk", "2026-10-30", "11:00", "12:00", "Позже"))
    points = asyncio.run(program.checkin_session_points("msk", datetime(2026, 10, 30, 10, 30)))
    assert [p["point"] for p in points] == [
        f"session:{id_now}", f"session:{id_now - 1}", f"session:{id_now + 1}",
    ]
    assert points[0]["live"] is True
    assert points[1]["live"] is False
    assert points[2]["live"] is False


def test_checkin_session_points_carries_capacity(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("msk", "Большой зал", capacity=120))
    sid = asyncio.run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Открытие", hall_id=hall_id,
    ))
    points = asyncio.run(program.checkin_session_points("msk", datetime(2026, 10, 30, 10, 30)))
    assert points == [{
        "point": f"session:{sid}", "label": "10:00–11:00 · Большой зал · Открытие",
        "live": True, "capacity": 120,
    }]


# ── scanned_outside_session_window (форум-ночь п.5) ──────────────────────────────────────────

def test_scanned_outside_session_window_none_scanned_at_is_false():
    session = {"day": "2026-10-30", "start_time": "10:00", "end_time": "11:00"}
    assert program.scanned_outside_session_window(session, None) is False


def test_scanned_outside_session_window_inside_slack_is_false():
    session = {"day": "2026-10-30", "start_time": "10:00", "end_time": "11:00"}
    assert program.scanned_outside_session_window(session, "2026-10-30 09:45:00") is False
    assert program.scanned_outside_session_window(session, "2026-10-30 11:25:00") is False


def test_scanned_outside_session_window_outside_slack_is_true():
    session = {"day": "2026-10-30", "start_time": "10:00", "end_time": "11:00"}
    assert program.scanned_outside_session_window(session, "2026-10-30 09:00:00") is True
    assert program.scanned_outside_session_window(session, "2026-10-30 12:00:00") is True


def test_scanned_outside_session_window_other_day_is_false():
    session = {"day": "2026-10-30", "start_time": "10:00", "end_time": "11:00"}
    assert program.scanned_outside_session_window(session, "2026-10-31 10:30:00") is False


# ── copy_program_day ──────────────────────────────────────────────────────────────────────────

def test_copy_program_day_creates_halls_and_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    hall_id = asyncio.run(db.create_program_hall("spb", "Большой зал", capacity=100))
    asyncio.run(db.create_program_session(
        "spb", "2026-10-03", "10:00", "11:00", "Открытие",
        speaker="Иван", hall_id=hall_id, description="Вступление",
    ))
    asyncio.run(db.create_program_session(
        "spb", "2026-10-03", "11:00", "12:00", "Без зала",
    ))

    stats = asyncio.run(program.copy_program_day("spb", "tyumen", "2026-10-03"))
    assert stats == {"halls_created": 1, "sessions_created": 2}

    tyumen_sessions = asyncio.run(db.list_program_sessions_for_city_day("tyumen", "2026-10-03"))
    assert len(tyumen_sessions) == 2
    opening = next(s for s in tyumen_sessions if s["title"] == "Открытие")
    assert opening["speaker"] == "Иван"
    assert opening["description"] == "Вступление"
    assert opening["hall_id"] is not None
    tyumen_hall = asyncio.run(db.get_program_hall(opening["hall_id"]))
    assert tyumen_hall["name"] == "Большой зал"
    assert tyumen_hall["capacity"] == 100

    without_hall = next(s for s in tyumen_sessions if s["title"] == "Без зала")
    assert without_hall["hall_id"] is None

    # Исходный город (СПб) не тронут -- копирование не переносит, а дублирует.
    spb_sessions = asyncio.run(db.list_program_sessions_for_city_day("spb", "2026-10-03"))
    assert len(spb_sessions) == 2


def test_copy_program_day_reuses_existing_hall_by_name(tmp_path):
    _use_tmp_db(tmp_path)
    src_hall = asyncio.run(db.create_program_hall("spb", "Большой зал"))
    dest_hall = asyncio.run(db.create_program_hall("tyumen", "Большой зал"))
    asyncio.run(db.create_program_session(
        "spb", "2026-10-03", "10:00", "11:00", "Открытие", hall_id=src_hall,
    ))

    stats = asyncio.run(program.copy_program_day("spb", "tyumen", "2026-10-03"))
    assert stats["halls_created"] == 0  # зал переиспользован, не создан заново

    halls = asyncio.run(db.list_program_halls("tyumen"))
    assert len(halls) == 1  # не задвоился
    tyumen_sessions = asyncio.run(db.list_program_sessions_for_city_day("tyumen", "2026-10-03"))
    assert tyumen_sessions[0]["hall_id"] == dest_hall


def test_copy_program_day_does_not_remove_existing_destination_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.create_program_session(
        "tyumen", "2026-10-03", "09:00", "09:30", "Уже было",
    ))
    asyncio.run(db.create_program_session(
        "spb", "2026-10-03", "10:00", "11:00", "Открытие",
    ))
    asyncio.run(program.copy_program_day("spb", "tyumen", "2026-10-03"))
    tyumen_sessions = asyncio.run(db.list_program_sessions_for_city_day("tyumen", "2026-10-03"))
    titles = {s["title"] for s in tyumen_sessions}
    assert titles == {"Уже было", "Открытие"}


def test_copy_program_day_empty_source_day_is_a_noop(tmp_path):
    _use_tmp_db(tmp_path)
    stats = asyncio.run(program.copy_program_day("spb", "tyumen", "2026-10-03"))
    assert stats == {"halls_created": 0, "sessions_created": 0}
