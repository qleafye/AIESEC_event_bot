"""Форум-ночь п.5 (FORUM-CHECKIN.md D-18..D-20): отметка на СЕССИЯХ программы — единая точка
`services.checkin.record_arrival`, которая решает, какой БД-путь нужен (`database.db.
record_checkin` для «Входа», `database.db.record_session_checkin` для `session:{id}`), проверяет
город делегата против города сессии и авто-подтверждает вход (`source="auto_session"`), если
его ещё не было.

БД — шаблонная копия через `tests/_dbtpl.py::fast_init_db`, `asyncio.run()` — pytest-asyncio
недоступен в этом окружении (тот же приём, что у соседних тестов чек-ина/программы).
`config.GOOGLE_SHEET_ID`/`GOOGLE_CREDENTIALS_FILE` не заданы в тестовом окружении —
`mark_arrived_in_sheet` внутри `record_arrival` fail-soft уходит в no-op (см.
`services.sheets.update_arrived_in_sheet`), отдельного мока не нужно (тот же приём, что у
`tests/test_admin_checkin_260924.py`)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from services.checkin import ENTRY_POINT, record_arrival
from tests._dbtpl import fast_init_db

UID = 260925001


def _use_tmp_db(tmp_path, name="test_checkin_sessions_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _seed_user(uid, city="msk", full_name="Иванов Иван", status="approved"):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city, "status": status,
    }))


def _run(coro):
    return asyncio.run(coro)


# ── entry point -- passthrough один-в-один со старым record_checkin ────────────────────────

def test_record_arrival_entry_point_is_new_then_duplicate(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    user = _run(db.get_user(UID))
    r1 = _run(record_arrival(user, ENTRY_POINT, source="miniapp"))
    assert r1["status"] == "new"
    r2 = _run(record_arrival(user, ENTRY_POINT, source="miniapp"))
    assert r2["status"] == "duplicate"
    assert r2["scanned_at"] == r1["scanned_at"]


# ── session point -- город делегата обязан совпасть с городом сессии (D-18) ─────────────────

def test_record_arrival_wrong_city_is_denied_with_human_reason(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="spb")
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    user = _run(db.get_user(UID))
    result = _run(record_arrival(user, f"session:{sid}", source="miniapp"))
    assert result["status"] == "wrong_city"
    assert "spb" not in result["reason_text"]  # человеческая метка, не код города
    assert result["reason_text"]
    # ничего не отмечено -- ни на сессии, ни на входе (auto_session не сработал)
    assert _run(db.count_checkins_by_point(f"session:{sid}")) == 0
    assert _run(db.count_checkins_by_point(ENTRY_POINT)) == 0


def test_record_arrival_same_city_session_is_allowed(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    user = _run(db.get_user(UID))
    result = _run(record_arrival(user, f"session:{sid}", source="miniapp"))
    assert result["status"] == "new"


# ── session point -- авто-вход (source="auto_session") ───────────────────────────────────────

def test_record_arrival_session_checkin_auto_marks_entry(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    user = _run(db.get_user(UID))
    _run(record_arrival(user, f"session:{sid}", source="miniapp"))
    assert _run(db.count_checkins_by_point(ENTRY_POINT)) == 1
    async def _source():
        async with db._connect() as conn:
            conn.row_factory = db.aiosqlite.Row
            async with conn.execute(
                "SELECT source FROM checkins WHERE telegram_id = ? AND point = ?",
                (UID, ENTRY_POINT),
            ) as cur:
                row = await cur.fetchone()
        return row["source"]
    assert _run(_source()) == "auto_session"


def test_record_arrival_session_checkin_does_not_override_existing_entry(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    user = _run(db.get_user(UID))
    _run(record_arrival(user, ENTRY_POINT, source="csv", scanned_at="2026-10-30 08:00:00"))
    _run(record_arrival(user, f"session:{sid}", source="miniapp", scanned_at="2026-10-30 10:05:00"))
    # первая (настоящая) отметка на входе не переписана авто-входом от сессии (D-10 first-wins).
    async def _entry_ts():
        async with db._connect() as conn:
            conn.row_factory = db.aiosqlite.Row
            async with conn.execute(
                "SELECT scanned_at FROM checkins WHERE telegram_id = ? AND point = ?",
                (UID, ENTRY_POINT),
            ) as cur:
                row = await cur.fetchone()
        return row["scanned_at"]
    assert _run(_entry_ts()) == "2026-10-30 08:00:00"


# ── session point -- D-20 «последний скан слота засчитывается» ──────────────────────────────

def test_record_arrival_moved_between_parallel_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    sid1 = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Зал А"))
    sid2 = _run(db.create_program_session("msk", "2026-10-30", "10:30", "11:30", "Зал Б"))
    user = _run(db.get_user(UID))
    _run(record_arrival(user, f"session:{sid1}", source="miniapp", scanned_at="2026-10-30 10:05:00"))
    result = _run(record_arrival(user, f"session:{sid2}", source="miniapp", scanned_at="2026-10-30 10:35:00"))
    assert result["status"] == "moved"
    assert result["previous_title"] == "Зал А"
    assert _run(db.count_checkins_by_point(f"session:{sid1}")) == 0
    assert _run(db.count_checkins_by_point(f"session:{sid2}")) == 1


def test_record_arrival_duplicate_same_session(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    user = _run(db.get_user(UID))
    r1 = _run(record_arrival(user, f"session:{sid}", source="miniapp", scanned_at="2026-10-30 10:00:00"))
    r2 = _run(record_arrival(user, f"session:{sid}", source="miniapp", scanned_at="2026-10-30 10:20:00"))
    assert r1["status"] == "new"
    assert r2["status"] == "duplicate"
    assert r2["scanned_at"] == "2026-10-30 10:00:00"


def test_record_arrival_non_parallel_sessions_do_not_evict_each_other(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    sid1 = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Утро"))
    sid2 = _run(db.create_program_session("msk", "2026-10-30", "11:00", "12:00", "День"))
    user = _run(db.get_user(UID))
    _run(record_arrival(user, f"session:{sid1}", source="miniapp"))
    result = _run(record_arrival(user, f"session:{sid2}", source="miniapp"))
    assert result["status"] == "new"  # разные слоты -- не перенос, обе отметки остаются
    assert _run(db.count_checkins_by_point(f"session:{sid1}")) == 1
    assert _run(db.count_checkins_by_point(f"session:{sid2}")) == 1


def test_record_arrival_invalid_session_id_is_safe(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="msk")
    user = _run(db.get_user(UID))
    result = _run(record_arrival(user, "session:999999", source="miniapp"))
    assert result["status"] == "invalid_point"
