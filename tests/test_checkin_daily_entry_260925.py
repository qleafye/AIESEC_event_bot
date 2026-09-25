"""Вход каждый день (двухдневный форум в Москве): отметка входа уникальна по (делегат, точка,
день по Москве), а не одна на форум.

- миграция живой базы: старая `checkins` с UNIQUE(telegram_id, point) пересоздаётся с колонкой
  `day` (старые строки — день из `scanned_at`), счётчик AUTOINCREMENT сохраняется, повторный
  `init_db()` ничего не делает;
- `record_checkin`: тот же день — "duplicate", другой день — "new";
- фильтр рассылки «Отметка на форуме» с днём и шаблон «Не пришёл» — по сегодняшнему входу;
- лист «Пришёл» — время ПЕРВОГО входа за форум; снятие входа пересчитывает ячейку;
- счётчик «Пришли» — одобренные текущего сезона, за день и за форум."""
from __future__ import annotations

import asyncio
from datetime import datetime

import aiosqlite

from config import config
from database import db
from services import checkin as checkin_mod
from services import checkin_arrival, venue_log
from services import timeutil as timeutil_mod
from tests._dbtpl import fast_init_db

UID = 260925101
UID2 = 260925102

# Схема `checkins` до этого изменения (317e1dd:database/db.py) — ровно то, что лежит на стенде/проде.
_OLD_CHECKINS_DDL = '''
    CREATE TABLE checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        point TEXT NOT NULL,
        scanned_at TEXT NOT NULL,
        source TEXT NOT NULL,
        approx_time INTEGER NOT NULL DEFAULT 0,
        by_staff_id INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(telegram_id, point)
    )
'''


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="daily_entry.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


async def _user(tid, *, status="approved", season=None):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season) VALUES (?, ?, ?, ?)",
            (tid, f"Тест {tid}", status, season),
        )
        await conn.commit()


# ── миграция ────────────────────────────────────────────────────────────────────────────────

async def _seed_old_schema(path: str):
    async with aiosqlite.connect(path) as conn:
        await conn.execute(_OLD_CHECKINS_DDL)
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at) "
            "VALUES (?, 'entry', '2026-10-30 09:10:00', 'miniapp', '2026-10-30 09:10:01')", (UID,),
        )
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, approx_time, created_at) "
            "VALUES (?, 'session:7', '2026-10-30 10:05:00', 'csv', 1, '2026-10-30 12:00:00')", (UID,),
        )
        # Удалённая строка: счётчик AUTOINCREMENT ушёл дальше, чем MAX(id).
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at) "
            "VALUES (?, 'entry', '2026-10-30 09:20:00', 'manual', '2026-10-30 09:20:00')", (UID2,),
        )
        await conn.execute("DELETE FROM checkins WHERE telegram_id = ?", (UID2,))
        await conn.commit()


def test_migration_rebuilds_old_table_and_is_idempotent(tmp_path):
    config.DB_PATH = str(tmp_path / "old_schema.db")
    _run(_seed_old_schema(config.DB_PATH))
    _run(db.init_db())

    async def snapshot():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM checkins ORDER BY id") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            async with conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'checkins'") as cur:
                seq = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'checkins'"
            ) as cur:
                ddl = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'checkins'"
            ) as cur:
                indexes = {r[0] for r in await cur.fetchall()}
        return rows, seq, ddl, indexes

    rows, seq, ddl, indexes = _run(snapshot())
    assert [(r["telegram_id"], r["point"], r["day"], r["source"], r["approx_time"]) for r in rows] == [
        (UID, "entry", "2026-10-30", "miniapp", 0),
        (UID, "session:7", "2026-10-30", "csv", 1),
    ]
    assert rows[0]["created_at"] == "2026-10-30 09:10:01"
    assert seq == 3
    assert "UNIQUE(telegram_id, point, day)" in ddl
    assert "idx_checkins_point" in indexes

    _run(db.init_db())  # второй init_db — no-op
    assert _run(snapshot()) == (rows, seq, ddl, indexes)

    # Новая строка не переиспользует id удалённой (счётчик сохранён).
    assert _run(db.record_checkin(UID, "entry", source="miniapp", scanned_at="2026-10-31 09:00:00")) == (
        "new", "2026-10-31 09:00:00",
    )
    assert _run(db.list_checkins_for_user(UID))[1]["id"] == 4
    # Тот же день, что у мигрированной строки, — повтор, время первой отметки.
    assert _run(db.record_checkin(UID, "entry", source="miniapp", scanned_at="2026-10-30 15:00:00")) == (
        "duplicate", "2026-10-30 09:10:00",
    )


# ── record_checkin по дням ──────────────────────────────────────────────────────────────────

def test_entry_is_unique_per_day(tmp_path):
    _ready(tmp_path)
    assert _run(db.record_checkin(UID, "entry", source="miniapp", scanned_at="2026-10-30 09:00:00"))[0] == "new"
    assert _run(db.record_checkin(UID, "entry", source="miniapp", scanned_at="2026-10-30 18:00:00")) == (
        "duplicate", "2026-10-30 09:00:00",
    )
    assert _run(db.record_checkin(UID, "entry", source="miniapp", scanned_at="2026-10-31 09:30:00"))[0] == "new"
    assert _run(db.count_checkins_by_point("entry")) == 1  # людей, не строк
    assert _run(db.count_checkins_by_point("entry", day="2026-10-31")) == 1
    assert _run(db.count_checkins_by_point("entry", day="2026-11-01")) == 0
    assert _run(db.first_entry_scanned_at(UID)) == "2026-10-30 09:00:00"
    assert _run(db.has_entry_on_other_day(UID, "2026-10-31")) is True


def test_auto_session_entry_takes_session_scan_day(tmp_path):
    """CSV сессии второго дня, загруженный позже, ставит вход ВТОРОГО дня, а не дня загрузки."""
    _ready(tmp_path)
    _run(db.add_user({"telegram_id": UID, "full_name": "Тест", "registration_date": "2026-01-01",
                      "event_city": "msk", "status": "approved"}))
    user = _run(db.get_user(UID))
    sid = _run(db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "Второй день"))
    _run(checkin_mod.record_arrival(user, "entry", source="csv", scanned_at="2026-10-30 09:00:00"))
    _run(checkin_mod.record_arrival(user, f"session:{sid}", source="csv", scanned_at="2026-10-31 10:05:00"))
    entries = [(r["day"], r["source"]) for r in _run(db.list_checkins_for_user(UID)) if r["point"] == "entry"]
    assert entries == [("2026-10-30", "csv"), ("2026-10-31", "auto_session")]


# ── рассылки: фильтр «Отметка на форуме» с днём и шаблон «Не пришёл» ───────────────────────

def test_broadcast_filter_by_day_and_today(tmp_path, monkeypatch):
    _ready(tmp_path)
    for tid in (1, 2, 3):
        _run(_user(tid))
    _run(db.record_checkin(1, "entry", source="miniapp", scanned_at="2026-10-30 09:00:00"))
    _run(db.record_checkin(1, "entry", source="miniapp", scanned_at="2026-10-31 09:00:00"))
    _run(db.record_checkin(2, "entry", source="miniapp", scanned_at="2026-10-30 09:05:00"))
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 31, 11, 0))

    def ids(**f):
        return sorted(_run(db.count_and_list_filtered([{"field": "checkin_entry", **f}])))

    assert ids(value=db.CHECKIN_YES) == [1, 2]  # хоть один день
    assert ids(value=db.CHECKIN_NO) == [3]  # ни разу
    assert ids(value=db.CHECKIN_YES, day="2026-10-30") == [1, 2]
    assert ids(value=db.CHECKIN_NO, day="2026-10-31") == [2, 3]
    assert ids(value=db.CHECKIN_YES, day=db.CHECKIN_DAY_TODAY) == [1]
    assert ids(value=db.CHECKIN_NO, day=db.CHECKIN_DAY_TODAY) == [2, 3]
    assert _run(db.get_checkin_entry_days()) == ["2026-10-30", "2026-10-31"]
    # «Не пришёл» — по сегодняшнему входу: пришедший вчера, но не сегодня — кандидат.
    assert sorted(_run(db.checkin_not_arrived_pending_ids())) == [2, 3]


# ── лист «Пришёл» ──────────────────────────────────────────────────────────────────────────

def test_sheet_keeps_first_entry_and_recomputes_on_revoke(tmp_path, monkeypatch):
    """Лист пишет джоба очереди (services/sheet_arrival_sync.py) — после каждой отметки
    прогоняем её проход и смотрим, что ушло в лист."""
    _ready(tmp_path)
    writes: list[tuple[int, str]] = []

    async def fake_batch(id_to_value):
        writes.extend(sorted(id_to_value.items()))
        return {"written": set(id_to_value), "missing": set(), "failed": {}}

    import services.sheets as sheets
    from services import sheet_arrival_sync
    monkeypatch.setattr(sheets, "write_arrivals_batch", fake_batch)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")

    async def mark(stamp):
        status, ts = await db.record_checkin(UID, "entry", source="miniapp", scanned_at=stamp)
        await checkin_mod.mark_arrived_in_sheet(UID, status, ts)
        await sheet_arrival_sync.drain()

    _run(mark("2026-10-31 09:00:00"))  # живой скан второго дня
    _run(mark("2026-10-30 09:15:00"))  # CSV первого дня загрузили позже — он раньше, пишем его
    _run(mark("2026-11-01 09:00:00"))  # третий день — ячейку не трогаем
    assert writes == [(UID, "31.10 09:00"), (UID, "30.10 09:15")]

    rows = {r["day"]: r for r in _run(db.list_checkins_for_user(UID))}
    writes.clear()
    _run(venue_log.revoke_mark(rows["2026-10-30"]["id"], staff_id=1, staff_name="Менеджер"))
    _run(sheet_arrival_sync.drain())
    assert writes == [(UID, "31.10 09:00")]  # первый из оставшихся
    assert sorted(_run(db.list_checkins_for_user(UID)), key=lambda r: r["day"])[0]["day"] == "2026-10-31"


# ── счётчик «Пришли» ───────────────────────────────────────────────────────────────────────

def test_arrived_counts_use_approved_current_season(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL'26"))
    _run(_user(1, season="YL'26"))
    _run(_user(2, season="YL'26"))
    _run(_user(3, status="pending"))
    _run(_user(4, season="YL'25"))
    for tid in (1, 3, 4):  # неодобренный и прошлый сезон со входом — не в счёт
        _run(db.record_checkin(tid, "entry", source="manual", scanned_at="2026-10-30 09:00:00"))
    _run(db.record_checkin(2, "entry", source="manual", scanned_at="2026-10-31 09:00:00"))

    assert _run(checkin_arrival.arrived_counts(None)) == (2, 2)
    assert _run(checkin_arrival.arrived_counts(None, "2026-10-30")) == (1, 2)

    monkeypatch.setattr(timeutil_mod, "msk_now", lambda: datetime(2026, 10, 31, 12, 0))
    assert _run(checkin_arrival.counter_day()) == "2026-10-31"
    monkeypatch.setattr(timeutil_mod, "msk_now", lambda: datetime(2026, 11, 2, 12, 0))
    assert _run(checkin_arrival.counter_day()) is None


# ── фильтр рассылки: видимость кнопки и разбор дня в хендлере ──────────────────────────────

def test_second_day_everyone_came_yesterday_button_visible(tmp_path, monkeypatch):
    """Москва, день 2: все одобренные пришли в день 1, сегодня ещё никто. У каждого варианта
    одна сторона пустая, но кнопка «Отметка на форуме» видна, «не пришли сегодня» = все."""
    from handlers import admin_broadcasts
    from tests.test_roles_phase8 import FakeCallback, FakeMessage, _fresh_state

    _ready(tmp_path)
    config.ADMIN_IDS = [1]
    for tid in (1, 2):
        _run(_user(tid))
        _run(db.record_checkin(tid, "entry", source="miniapp", scanned_at="2026-10-30 09:00:00"))
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 31, 8, 30))

    msg = FakeMessage()
    _run(admin_broadcasts._render_filter_menu(msg, [], edit=False))
    flat = [b.callback_data for row in msg.answers[-1][2].inline_keyboard for b in row]
    assert "filter_f_checkin_entry" in flat

    cb = FakeCallback("filter_f_checkin_entry", 1)
    state = _fresh_state(1)
    _run(state.update_data(filters=[], filter_pending_field="checkin_entry"))
    _run(admin_broadcasts._show_value_picker(cb, state, "checkin_entry", "Выберите значение:"))
    data = _run(state.get_data())
    no_today = f"{db.CHECKIN_NO}@{db.CHECKIN_DAY_TODAY}"
    # Только варианты с людьми: «не пришли ни разу» и «пришли сегодня» пусты — их нет.
    assert data["filter_options"] == [db.CHECKIN_YES, no_today, f"{db.CHECKIN_YES}@2026-10-30"]
    assert data["filter_option_labels"][no_today] == "не пришли сегодня"

    idx = data["filter_options"].index(no_today)
    _run(admin_broadcasts.filter_pick_value(FakeCallback(f"filter_opt:{idx}", 1), state))
    filters = _run(state.get_data())["filters"]
    assert filters == [{"field": "checkin_entry", "value": db.CHECKIN_NO,
                        "label": "не пришли сегодня", "day": db.CHECKIN_DAY_TODAY}]
    assert sorted(_run(db.count_and_list_filtered(filters))) == [1, 2]


def test_filter_pick_value_parses_concrete_day(tmp_path):
    from handlers import admin_broadcasts
    from tests.test_roles_phase8 import FakeCallback, _fresh_state

    _ready(tmp_path)
    config.ADMIN_IDS = [1]
    state = _fresh_state(1)
    opt = f"{db.CHECKIN_YES}@2026-10-30"
    _run(state.update_data(
        filter_pending_field="checkin_entry", filters=[], filter_options=[db.CHECKIN_NO, opt],
        filter_option_labels={db.CHECKIN_NO: "не пришли ни разу", opt: "пришли 30.10"},
    ))
    _run(admin_broadcasts.filter_pick_value(FakeCallback("filter_opt:1", 1), state))
    assert _run(state.get_data())["filters"] == [
        {"field": "checkin_entry", "value": db.CHECKIN_YES, "label": "пришли 30.10", "day": "2026-10-30"},
    ]


def test_no_entries_yet_hides_button(tmp_path):
    _ready(tmp_path)
    _run(_user(1))
    assert _run(db.get_checkin_entry_picker_options()) == []
