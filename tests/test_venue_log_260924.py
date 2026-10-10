"""Идеи №31/№32 бэклога чек-ина: журнал площадки (`venue_log`) и снятие ошибочной отметки —
слой БД + сервис (`database/db.py`, `services/venue_log.py`, врезка в
`services/checkin.record_arrival`).

БД — шаблонная копия `tests/_dbtpl.py::fast_init_db`, `asyncio.run()` (pytest-asyncio в этом
окружении нет) — тот же приём, что `tests/test_checkin_sessions_260924.py`."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from services import venue_log
from services.checkin import ENTRY_POINT, record_arrival
from tests._dbtpl import fast_init_db

UID = 260925101
OTHER = 260925102
STAFF = 7001
STAFF2 = 7002
DAY = "2026-10-30"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_venue_log_260924.db")
    fast_init_db()


def _user(uid=UID, city="msk", full_name="Иванов Иван"):
    _run(db.add_user({
        "telegram_id": uid, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city, "status": "approved",
    }))
    return _run(db.get_user(uid))


def _points(uid):
    return [r["point"] for r in _run(db.list_checkins_for_user(uid))]


# ── журнал живой отметки ─────────────────────────────────────────────────────────────────────

def test_live_entry_checkin_is_logged_with_staff_and_city(tmp_path):
    _ready(tmp_path)
    user = _user()
    r = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF, staff_name="Анна"))
    assert r["status"] == "new" and r["log_id"]
    row = _run(db.venue_log_get(r["log_id"]))
    assert row["action"] == "checkin"
    assert row["staff_id"] == STAFF and row["staff_name"] == "Анна"
    assert row["telegram_id"] == UID and row["city"] == "msk" and row["point"] == ENTRY_POINT
    assert row["details"]["scanned_at"] == r["scanned_at"]


def test_duplicate_and_csv_are_not_logged(tmp_path):
    _ready(tmp_path)
    user = _user()
    _run(record_arrival(user, ENTRY_POINT, source="csv", by_staff_id=STAFF))
    r2 = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    assert r2["status"] == "duplicate" and "log_id" not in r2
    rows, total = _run(db.venue_log_page())
    assert total == 0


# ── отмена своего последнего скана (волонтёр) ────────────────────────────────────────────────

def test_undo_own_last_entry_removes_mark_and_logs(tmp_path):
    _ready(tmp_path)
    user = _user()
    r = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF, staff_name="Анна"))
    assert _run(venue_log.undo_last_scan(STAFF, "Анна", r["log_id"])) == "ok"
    assert _points(UID) == []
    assert _run(db.venue_log_get(r["log_id"]))["undone_at"]
    rows, total = _run(db.venue_log_page())
    assert total == 2 and rows[0]["action"] == "undo" and rows[0]["telegram_id"] == UID
    # повторная отметка после отмены снова «new» (слушатели первой отметки позовутся ещё раз)
    again = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    assert again["status"] == "new"


def test_undo_twice_is_refused(tmp_path):
    _ready(tmp_path)
    user = _user()
    r = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "ok"
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "expired"


def test_undo_someone_elses_mark_is_refused(tmp_path):
    _ready(tmp_path)
    user = _user()
    r = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    assert _run(venue_log.undo_last_scan(STAFF2, None, r["log_id"])) == "not_yours"
    assert _points(UID) == [ENTRY_POINT]


def test_undo_not_last_mark_is_refused(tmp_path):
    _ready(tmp_path)
    first = _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    _run(record_arrival(_user(OTHER, full_name="Петров Пётр"), ENTRY_POINT, source="manual", by_staff_id=STAFF))
    assert _run(venue_log.undo_last_scan(STAFF, None, first["log_id"])) == "not_last"
    assert _points(UID) == [ENTRY_POINT]


def test_undo_after_window_is_refused(tmp_path, monkeypatch):
    _ready(tmp_path)
    r = _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    later = datetime.now() + timedelta(days=2)  # created_at пишется по Москве — берём заведомо позже
    monkeypatch.setattr(venue_log, "msk_now", lambda: later)
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "expired"
    assert _points(UID) == [ENTRY_POINT]


def test_undo_session_scan_removes_auto_entry_too(tmp_path):
    _ready(tmp_path)
    user = _user()
    sid = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Открытие"))
    r = _run(record_arrival(user, f"session:{sid}", source="miniapp", by_staff_id=STAFF,
                            scanned_at=f"{DAY} 10:05:00"))
    assert sorted(_points(UID)) == sorted([ENTRY_POINT, f"session:{sid}"])
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "ok"
    assert _points(UID) == []


def test_undo_session_scan_keeps_earlier_real_entry(tmp_path):
    _ready(tmp_path)
    user = _user()
    _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=STAFF2))
    sid = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Открытие"))
    r = _run(record_arrival(user, f"session:{sid}", source="miniapp", by_staff_id=STAFF,
                            scanned_at=f"{DAY} 10:05:00"))
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "ok"
    assert _points(UID) == [ENTRY_POINT]


def test_undo_moved_restores_previous_session(tmp_path):
    _ready(tmp_path)
    user = _user()
    a = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Зал А"))
    b = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Зал Б"))
    _run(record_arrival(user, f"session:{a}", source="miniapp", by_staff_id=STAFF2,
                        scanned_at=f"{DAY} 10:02:00"))
    r = _run(record_arrival(user, f"session:{b}", source="miniapp", by_staff_id=STAFF,
                            scanned_at=f"{DAY} 10:06:00"))
    assert r["status"] == "moved"
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "ok"
    rows = {row["point"]: row for row in _run(db.list_checkins_for_user(UID))}
    assert set(rows) == {ENTRY_POINT, f"session:{a}"}
    assert rows[f"session:{a}"]["scanned_at"] == f"{DAY} 10:02:00"
    assert rows[f"session:{a}"]["by_staff_id"] == STAFF2


def test_undo_when_mark_already_gone(tmp_path):
    _ready(tmp_path)
    r = _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    entry = _run(db.list_checkins_for_user(UID))[0]
    _run(venue_log.revoke_mark(entry["id"], staff_id=STAFF2, staff_name="Менеджер"))
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "gone"


# ── снятие менеджером ────────────────────────────────────────────────────────────────────────

def test_manager_revoke_entry_keeps_sessions(tmp_path):
    _ready(tmp_path)
    user = _user()
    sid = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Открытие"))
    _run(record_arrival(user, f"session:{sid}", source="miniapp", by_staff_id=STAFF,
                        scanned_at=f"{DAY} 10:05:00"))
    entry = next(r for r in _run(db.list_checkins_for_user(UID)) if r["point"] == ENTRY_POINT)
    removed = _run(venue_log.revoke_mark(entry["id"], staff_id=STAFF2, staff_name="Менеджер"))
    assert removed["point"] == ENTRY_POINT
    assert _points(UID) == [f"session:{sid}"]
    rows, _ = _run(db.venue_log_page())
    assert rows[0]["action"] == "revoke" and rows[0]["staff_id"] == STAFF2
    assert rows[0]["details"]["was_source"] == "auto_session"
    assert _run(venue_log.revoke_mark(entry["id"], staff_id=STAFF2, staff_name=None)) is None


def test_revoked_session_mark_gets_no_feedback_prompt(tmp_path):
    """Отзыв о сессии (D-24) берёт отмеченных в момент срабатывания джобы — снятая отметка
    туда не попадает, отдельно отменять джобу не нужно."""
    _ready(tmp_path)
    user = _user()
    sid = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Открытие"))
    _run(record_arrival(user, f"session:{sid}", source="miniapp", by_staff_id=STAFF,
                        scanned_at=f"{DAY} 10:05:00"))
    mark = next(r for r in _run(db.list_checkins_for_user(UID)) if r["point"] == f"session:{sid}")
    _run(venue_log.revoke_mark(mark["id"], staff_id=STAFF2, staff_name=None))
    assert UID not in _run(db.list_marked_telegram_ids_for_session(sid))
    assert _run(db.is_marked_for_session(UID, sid)) is False


# ── экран журнала: фильтры и строки ──────────────────────────────────────────────────────────

def test_page_filters_by_city_and_staff(tmp_path):
    import domain.cities as cities

    _ready(tmp_path)
    _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF, staff_name="Анна"))
    _run(record_arrival(_user(OTHER, city="spb", full_name="Петров Пётр"), ENTRY_POINT,
                        source="miniapp", by_staff_id=STAFF2, staff_name="Борис"))
    _run(venue_log.log_action(venue_log.ACTION_REISSUE_QR, staff_id=STAFF2, staff_name="Борис",
                              telegram_id=OTHER))
    _, total = _run(db.venue_log_page())
    assert total == 3
    rows, total = _run(db.venue_log_page(city_scope=cities.city_scope("spb")))
    assert total == 2 and {r["action"] for r in rows} == {"checkin", "reissue_qr"}
    rows, total = _run(db.venue_log_page(staff_id=STAFF))
    assert total == 1
    staff = _run(db.venue_log_staff())
    assert [(s["staff_id"], s["staff_name"], s["n"]) for s in staff] == [(STAFF2, "Борис", 2), (STAFF, "Анна", 1)]


def test_describe_is_human(tmp_path):
    _ready(tmp_path)
    r = _run(record_arrival(_user(), ENTRY_POINT, source="manual", by_staff_id=STAFF, staff_name="Анна (@anna)"))
    line = _run(venue_log.describe(_run(db.venue_log_get(r["log_id"]))))
    assert "Анна (@anna)" in line and "Иванов Иван" in line and "Вход" in line
    assert "поиск по фамилии" in line
    assert "manual" not in line and str(UID) not in line


def test_purge_user_removes_journal_rows_about_delegate(tmp_path):
    _ready(tmp_path)
    _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF))
    footprint = _run(db.count_user_footprint(UID))
    assert footprint["checkin"] >= 2  # отметка + строка журнала
    _run(db.purge_user(UID))
    _, total = _run(db.venue_log_page())
    assert total == 0


def test_undo_when_journal_no_longer_matches_mark_writes_nothing(tmp_path):
    """Исход — по rowcount DELETE: журнал говорит одно время скана, а строка отметки уже
    другая (её переставили) — отказ «gone», транзакция откатана, журнал не тронут."""
    _ready(tmp_path)
    r = _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF))

    async def _desync():
        async with db._connect() as conn:
            await conn.execute("UPDATE checkins SET scanned_at = '2000-01-01 00:00:00' WHERE telegram_id = ?", (UID,))
            await conn.commit()
    _run(_desync())
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "gone"
    assert _points(UID) == [ENTRY_POINT]
    assert _run(db.venue_log_get(r["log_id"]))["undone_at"] is None
    _, total = _run(db.venue_log_page())
    assert total == 1  # строки «undo» нет


def test_undo_with_empty_scanned_at_in_journal_is_refused(tmp_path):
    _ready(tmp_path)
    r = _run(record_arrival(_user(), ENTRY_POINT, source="miniapp", by_staff_id=STAFF))

    async def _blank():
        async with db._connect() as conn:
            await conn.execute("UPDATE venue_log SET details = '{}' WHERE id = ?", (r["log_id"],))
            await conn.commit()
    _run(_blank())
    assert _run(venue_log.undo_last_scan(STAFF, None, r["log_id"])) == "gone"
    assert _points(UID) == [ENTRY_POINT]
    _, total = _run(db.venue_log_page())
    assert total == 1
