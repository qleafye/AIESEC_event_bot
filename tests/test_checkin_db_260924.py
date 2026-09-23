"""Phase 12 (FORUM-CHECKIN.md, D-09/D-10/D-17): БД-слой отметки на форуме — таблица
`checkins` и `database.db.get_user_by_checkin_token`/`record_checkin`/
`count_checkins_by_point`/`count_approved_current_season`.

Токен (`get_or_create_checkin_token`) и формат содержимого QR (`services.checkin.build_payload`)
уже покрыты `tests/test_checkin_qr_260923.py` — здесь только новая половина: таблица `checkins`
(идемпотентность по (telegram_id, point), счётчики) и поиск делегата по токену.

БД — шаблонная копия через `tests/_dbtpl.py::fast_init_db` (см. докстринг модуля) вместо
полного прогона `init_db()` на каждый тест; `asyncio.run()` — тот же приём, что у
`test_checkin_qr_260923.py` (pytest-asyncio недоступен в этом окружении)."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

from config import config
from database import db
from tests._dbtpl import fast_init_db

UID = 260924001


def _use_tmp_db(tmp_path, name="test_checkin_db_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _seed_user(uid, city="Казань", full_name="Иванов Иван"):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city,
    }))


def _set_status(uid, status):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, uid))
    conn.commit()
    conn.close()


def _set_season(uid, season):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET season = ? WHERE telegram_id = ?", (season, uid))
    conn.commit()
    conn.close()


async def _set_setting(key, value):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value),
        )
        await conn.commit()


# ── get_user_by_checkin_token ────────────────────────────────────────────────────────────────

def test_get_user_by_checkin_token_roundtrip(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, full_name="Иванов Иван")
    tok = asyncio.run(db.get_or_create_checkin_token(UID))
    found = asyncio.run(db.get_user_by_checkin_token(tok))
    assert found is not None
    assert found["telegram_id"] == UID
    assert found["full_name"] == "Иванов Иван"


def test_get_user_by_checkin_token_unknown_or_empty_returns_none(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(db.get_user_by_checkin_token("no-such-token")) is None
    assert asyncio.run(db.get_user_by_checkin_token(None)) is None
    assert asyncio.run(db.get_user_by_checkin_token("")) is None


# ── record_checkin: идемпотентность по (telegram_id, point) (D-10) ─────────────────────────

def test_record_checkin_idempotent_first_wins(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    status1, ts1 = asyncio.run(db.record_checkin(UID, "entry", source="csv"))
    assert status1 == "new"
    status2, ts2 = asyncio.run(db.record_checkin(UID, "entry", source="csv"))
    assert status2 == "duplicate"
    assert ts1 == ts2  # первая отметка не переписывается второй (D-10)


def test_record_checkin_different_points_are_independent(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    status_a, _ = asyncio.run(db.record_checkin(UID, "entry", source="csv"))
    status_b, _ = asyncio.run(db.record_checkin(UID, "session-1", source="csv"))
    assert status_a == "new"
    assert status_b == "new"  # разные точки -- не дубли друг друга


def test_record_checkin_explicit_scanned_at_and_approx(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    status, ts = asyncio.run(
        db.record_checkin(UID, "entry", source="csv", scanned_at="2026-10-03 09:15:00", approx=True)
    )
    assert status == "new"
    assert ts == "2026-10-03 09:15:00"


# T-12-03 (Rule 1): две одновременные отметки в ОДНУ секунду больше не путаются -- new/duplicate
# решает `cursor.rowcount`, а не сравнение времени (см. докстринг `record_checkin`).

def test_record_checkin_concurrent_same_second_only_one_new(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    fixed = datetime(2026, 10, 3, 9, 0, 0)
    monkeypatch.setattr(db, "msk_now", lambda: fixed)

    n = 20

    async def _run_all():
        return await asyncio.gather(
            *[db.record_checkin(UID, "entry", source="miniapp") for _ in range(n)]
        )

    results = asyncio.run(_run_all())
    statuses = [r[0] for r in results]
    assert statuses.count("new") == 1, f"ровно одна отметка должна выиграть гонку, получили: {statuses}"
    assert statuses.count("duplicate") == n - 1

    stamp = "2026-10-03 09:00:00"
    # ВСЕ отметки (и "new", и "duplicate") обязаны показывать ОДНО и то же время первой
    # отметки -- дубли не подменяют время выигравшего конкурента своим собственным.
    for status, ts in results:
        assert ts == stamp, f"{status} отдал чужое/иное время: {ts!r}"

    assert asyncio.run(db.count_checkins_by_point("entry")) == 1  # физически одна строка, не n


# ── count_checkins_by_point / count_approved_current_season ────────────────────────────────

def test_count_checkins_by_point(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    _seed_user(UID + 1)
    assert asyncio.run(db.count_checkins_by_point("entry")) == 0
    asyncio.run(db.record_checkin(UID, "entry", source="csv"))
    asyncio.run(db.record_checkin(UID + 1, "entry", source="csv"))
    assert asyncio.run(db.count_checkins_by_point("entry")) == 2
    assert asyncio.run(db.count_checkins_by_point("session-1")) == 0


def test_count_approved_current_season_filters_status_and_season(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(_set_setting("event_season", "YL'26"))
    _seed_user(UID)
    _set_status(UID, "approved")
    _set_season(UID, "YL'26")
    _seed_user(UID + 1)
    _set_status(UID + 1, "approved")
    _set_season(UID + 1, None)  # season не проставлен -- считаем текущим
    _seed_user(UID + 2)
    _set_status(UID + 2, "approved")
    _set_season(UID + 2, "YL'25")  # прошлый сезон -- не считаем
    _seed_user(UID + 3)
    _set_status(UID + 3, "pending")
    _set_season(UID + 3, "YL'26")  # не одобрен -- не считаем
    n = asyncio.run(db.count_approved_current_season())
    assert n == 2


def test_count_approved_current_season_no_season_setting_counts_all_approved(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    _set_status(UID, "approved")
    _set_season(UID, "YL'25")
    _seed_user(UID + 1)
    _set_status(UID + 1, "approved")
    _set_season(UID + 1, None)
    n = asyncio.run(db.count_approved_current_season())
    assert n == 2  # event_season не задан -- fail-soft, сезон не фильтрует (как is_past_season_row)
