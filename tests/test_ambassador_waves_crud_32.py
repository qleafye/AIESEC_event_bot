"""Phase 32 План 10 (D-06/D-09/D-10/D-11/D-13): админка волн в чат-боте — правила сервиса
(даты/права/копия/редактируемые поля, задача 1), экран списка + визард создания (задача 2),
карточка волны — правка/копия/активация/удаление (задача 3).

pytest-asyncio недоступен — async через `asyncio.run()`, фикстура временной БД — тот же
приём, что `tests/test_ambassador_waves_db_32.py::_ready`.
"""
from __future__ import annotations

import asyncio

import pytest

import cities
from config import config
from database import db
from services import ambassador_waves as aw


ADMIN_ID = 921001
MSK_MANAGER_ID = 921002
SPB_MANAGER_ID = 921003
STRANGER_MANAGER_ID = 921004


def _ready(tmp_path, name="test_ambassador_waves_crud_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _codes():
    codes = cities.city_codes()
    assert len(codes) >= 2, "нужно хотя бы два города в тестовом реестре"
    return codes[0], codes[1]


def _bind_manager(manager_id: int, city: str):
    _run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    _run(db.set_staff_city(manager_id, city))


def _dt(day: str) -> str:
    """«01.10.2026» -> «2026-10-01 00:00:00» (начало дня, соглашение этого модуля)."""
    from datetime import datetime
    return datetime.strptime(day, "%d.%m.%Y").strftime("%Y-%m-%d 00:00:00")


def _dt_end(day: str) -> str:
    """«21.10.2026» -> «2026-10-21 23:59:59» (конец дня)."""
    from datetime import datetime
    return datetime.strptime(day, "%d.%m.%Y").strftime("%Y-%m-%d 23:59:59")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: validate_wave_dates / can_edit_wave / editable_city_codes / copy_wave /
# wave_editable_fields
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_editable_fields_four_states(tmp_path):
    _ready(tmp_path)
    draft = {"state": "draft"}
    active_fresh = {"state": "active", "started_notified_at": None}
    active_sent = {"state": "active", "started_notified_at": "2026-10-01 09:00:00"}
    closing = {"state": "closing"}
    announced = {"state": "announced"}
    assert aw.wave_editable_fields(draft) == {"dates", "intro_text", "prize_places", "event_city", "tasks"}
    assert aw.wave_editable_fields(active_fresh) == aw.wave_editable_fields(draft)
    assert aw.wave_editable_fields(active_sent) == {"intro_text", "prize_places"}
    assert aw.wave_editable_fields(closing) == {"prize_places"}
    assert aw.wave_editable_fields(announced) == set()


def test_validate_wave_dates_end_before_start(tmp_path):
    _ready(tmp_path)
    msg = _run(aw.validate_wave_dates(_dt_end("21.10.2026"), _dt("01.10.2026"), None))
    assert msg is not None
    assert "дат" in msg.lower()


def test_validate_wave_dates_zero_length(tmp_path):
    _ready(tmp_path)
    same = "2026-10-01 12:00:00"
    msg = _run(aw.validate_wave_dates(same, same, None))
    assert msg is not None


def test_validate_wave_dates_ok_no_conflict(tmp_path):
    _ready(tmp_path)
    msg = _run(aw.validate_wave_dates(_dt("01.10.2026"), _dt_end("10.10.2026"), None))
    assert msg is None


def test_validate_wave_dates_edge_overlap_caught_adjacent_not(tmp_path):
    _ready(tmp_path)
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    # Перекрытие ровно по краю (новая начинается в день конца старой) — считается пересечением.
    msg_edge = _run(aw.validate_wave_dates(_dt("10.10.2026"), _dt_end("15.10.2026"), None))
    assert msg_edge is not None
    assert str(wid) not in "".join([]) and "id" not in msg_edge.lower()
    # Соседний отрезок (новая начинается на следующий день после конца старой) — не пересекается.
    msg_adjacent = _run(aw.validate_wave_dates(_dt("11.10.2026"), _dt_end("15.10.2026"), None))
    assert msg_adjacent is None


def test_validate_wave_dates_message_has_no_db_ids(tmp_path):
    _ready(tmp_path)
    _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    msg = _run(aw.validate_wave_dates(_dt("05.10.2026"), _dt_end("15.10.2026"), None))
    assert msg is not None
    for forbidden in ("id", "wave_id", "None"):
        assert forbidden not in msg


def test_validate_wave_dates_exclude_id_allows_self(tmp_path):
    _ready(tmp_path)
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    # Правка ТОЙ ЖЕ волны на те же даты не должна конфликтовать сама с собой.
    msg = _run(aw.validate_wave_dates(_dt("01.10.2026"), _dt_end("10.10.2026"), None, exclude_id=wid))
    assert msg is None


def test_can_edit_wave_bound_manager_own_city_only(tmp_path):
    _ready(tmp_path)
    city_a, city_b = _codes()
    _bind_manager(MSK_MANAGER_ID, city_a)
    wave_own = {"event_city": city_a}
    wave_other = {"event_city": city_b}
    assert _run(aw.can_edit_wave(MSK_MANAGER_ID, wave_own)) is True
    assert _run(aw.can_edit_wave(MSK_MANAGER_ID, wave_other)) is False


def test_can_edit_wave_bound_manager_cannot_touch_all_cities_wave(tmp_path):
    _ready(tmp_path)
    city_a, _ = _codes()
    _bind_manager(MSK_MANAGER_ID, city_a)
    wave_all = {"event_city": None}
    assert _run(aw.can_edit_wave(MSK_MANAGER_ID, wave_all)) is False


def test_can_edit_wave_superadmin_sees_everything(tmp_path):
    _ready(tmp_path)
    city_a, city_b = _codes()
    assert _run(aw.can_edit_wave(ADMIN_ID, {"event_city": city_a})) is True
    assert _run(aw.can_edit_wave(ADMIN_ID, {"event_city": city_b})) is True
    assert _run(aw.can_edit_wave(ADMIN_ID, {"event_city": None})) is True


def test_copy_wave_shifts_deadlines_by_wave_start_delta(tmp_path):
    _ready(tmp_path)
    src_id = _run(db.create_wave(
        _dt("01.10.2026"), _dt_end("10.10.2026"), intro_text="Стартуем!", prize_places=5,
        created_by=ADMIN_ID,
    ))
    t1 = _run(db.create_task(
        "Пост в сторис", "Light", 10, "photo", "2026-10-05 12:00:00", ADMIN_ID,
        wave_id=src_id, audience="ambassadors",
    ))
    t2 = _run(db.create_task(
        "Без срока", "Hard", 50, "text", db.NO_DEADLINE_AT, ADMIN_ID,
        wave_id=src_id, audience="all",
    ))
    new_starts = _dt("15.11.2026")
    new_ends = _dt_end("24.11.2026")
    new_id = _run(aw.copy_wave(src_id, new_starts, new_ends, created_by=ADMIN_ID))

    new_wave = _run(db.get_wave(new_id))
    assert new_wave["intro_text"] == "Стартуем!"
    assert new_wave["prize_places"] == 5
    assert new_wave["state"] == "draft"

    tasks = _run(db.list_wave_tasks(new_id, active_only=False))
    assert len(tasks) == 2

    from datetime import datetime
    old_start = datetime.strptime("2026-10-01 00:00:00", "%Y-%m-%d %H:%M:%S")
    new_start = datetime.strptime(new_starts, "%Y-%m-%d %H:%M:%S")
    shift = new_start - old_start
    old_deadline = datetime.strptime("2026-10-05 12:00:00", "%Y-%m-%d %H:%M:%S")
    expected = (old_deadline + shift).strftime("%Y-%m-%d %H:%M:%S")

    by_text = {t["text"]: t for t in tasks}
    assert by_text["Пост в сторис"]["deadline_at"] == expected
    assert by_text["Без срока"]["deadline_at"] == db.NO_DEADLINE_AT


def test_copy_wave_clamps_deadline_to_new_wave_end(tmp_path):
    _ready(tmp_path)
    src_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("31.10.2026"), created_by=ADMIN_ID))
    # Дедлайн задания в последний день старой волны -> после сдвига на короткую новую волну
    # вылезет за её конец и должен быть подрезан ровно до конца новой волны.
    _run(db.create_task(
        "Позднее задание", "Medium", 20, "text", "2026-10-31 20:00:00", ADMIN_ID, wave_id=src_id,
    ))
    new_starts = _dt("01.11.2026")
    new_ends = _dt_end("03.11.2026")
    new_id = _run(aw.copy_wave(src_id, new_starts, new_ends, created_by=ADMIN_ID))
    tasks = _run(db.list_wave_tasks(new_id, active_only=False))
    assert tasks[0]["deadline_at"] == new_ends


def test_copy_wave_skips_archived_tasks(tmp_path):
    _ready(tmp_path)
    src_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    tid = _run(db.create_task(
        "В архиве", "Light", 5, "text", "2026-10-05 12:00:00", ADMIN_ID, wave_id=src_id,
    ))
    _run(db.archive_task(tid))
    new_id = _run(aw.copy_wave(src_id, _dt("01.11.2026"), _dt_end("10.11.2026"), created_by=ADMIN_ID))
    tasks = _run(db.list_wave_tasks(new_id, active_only=False))
    assert tasks == []
