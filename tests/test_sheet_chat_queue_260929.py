"""Колонка «В чате»: живые изменения — через очередь `sheet_chat_queue` (29.09).

- `upsert_chat_member` одобренного делегата ставит событие только при смене присутствия;
  неодобренный/незарегистрированный — нет; сбой вставки не ломает учёт чата;
- `approve_user` и `refresh_chat` ставят события;
- `services/sheet_chat_sync.drain`: две вкладки -> ровно 2 batch_get и 2 batch_update, дубли
  схлопнуты, значение из базы; строки нет -> событие снято; сбой -> backoff, ошибка без
  секретов; лист без колонки «В чате» -> событие снято, другие колонки не тронуты."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import gspread

from config import config
from database import db
import services.sheets as sheets
from services import chat_tracking, sheet_chat_sync
from tests._dbtpl import fast_init_db
from tests.test_sheet_arrival_queue_260925 import QueueFakeWorksheet
from tests.test_sheet_status_city_tab_260819 import _patch_fake_sheets

CHAT = -100555
HEADER = ["id", sheets.STATUS_HEADER, sheets.ARRIVED_HEADER, sheets.CHAT_HEADER]


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "chatq.db")
    fast_init_db()


async def _queue() -> list[dict]:
    return await db.list_due_sheet_chat("9999-12-31 00:00:00", 100_000)


async def _user(tid, city=None, status="approved"):
    await db.add_user({"telegram_id": tid, "event_city": city, "participant_type": "full",
                       "registration_date": "2026-09-01T00:00:00"})
    await db.set_user_status(tid, status)


# ── постановка событий ─────────────────────────────────────────────────────────────────────

def test_upsert_enqueues_only_on_presence_change(tmp_path):
    _ready(tmp_path)

    async def go():
        await _user(1)
        seen = []
        for status in ("member", "member", "left", "left", "member", "administrator"):
            await db.upsert_chat_member(CHAT, 1, status, source="chat_member")
            seen.append(len(await _queue()))
        return seen

    # нет записи -> member (+1), member -> member (0), -> left (+1), left -> left (0),
    # -> member (+1), member -> administrator (присутствие не менялось, 0)
    assert _run(go()) == [1, 1, 2, 2, 3, 3]


def test_upsert_first_absent_record_is_not_event(tmp_path):
    """Первая запись «left» — присутствия не было и нет, ячейка «не проверено» -> «нет»
    всё же меняется, поэтому событие ставится и на первую запись любого статуса."""
    _ready(tmp_path)

    async def go():
        await _user(1)
        await db.upsert_chat_member(CHAT, 1, "left", source="refresh")
        return len(await _queue())

    assert _run(go()) == 1


def test_upsert_skips_not_approved_and_unknown(tmp_path):
    _ready(tmp_path)

    async def go():
        await _user(2, status="pending")
        await db.upsert_chat_member(CHAT, 2, "member", source="chat_member")
        await db.upsert_chat_member(CHAT, 999, "member", source="chat_member")
        return await _queue()

    assert _run(go()) == []


def test_upsert_is_fail_soft_without_queue_table(tmp_path):
    _ready(tmp_path)

    async def go():
        await _user(1)
        async with db._connect() as conn:
            await conn.execute("DROP TABLE sheet_chat_queue")
            await conn.commit()
        await db.upsert_chat_member(CHAT, 1, "member", source="chat_member")
        return await db.chat_member_row(CHAT, 1)

    row = _run(go())
    assert row["status"] == "member"


def test_approve_user_enqueues(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import reg_schema

    async def fake_send(*a, **k):
        return None

    monkeypatch.setattr(reg_schema, "send_completion_and_bonus", fake_send)

    async def go():
        await _user(5)
        await reg_schema.approve_user(SimpleNamespace(), 5)
        return [r["telegram_id"] for r in await _queue()]

    assert _run(go()) == [5]


class _Bot:
    id = 1

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


def test_refresh_chat_enqueues_all_approved_of_city(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        await chat_tracking.bind_chat(None, CHAT, "Чат", None)
        for tid in (1, 2, 3):
            await _user(tid)
        await db.upsert_chat_member(CHAT, 3, "member", source="refresh")  # свежая, не спросят
        async with db._connect() as conn:
            await conn.execute("DELETE FROM sheet_chat_queue")
            await conn.commit()
        await chat_tracking.refresh_chat(_Bot(), CHAT, None)
        return sorted({r["telegram_id"] for r in await _queue()})

    assert _run(go()) == [1, 2, 3]


# ── разбор очереди ────────────────────────────────────────────────────────────────────────

def _tabs(monkeypatch, main_rows, spb_rows, header=HEADER):
    main = QueueFakeWorksheet("main", rows=main_rows, header=header)
    spb = QueueFakeWorksheet("СПб", rows=spb_rows, header=header)
    tabs = {"__main__": main, "СПб": spb}
    _patch_fake_sheets(monkeypatch, tabs)
    monkeypatch.setattr(sheets, "_open_named_or_main_sync", lambda t: tabs.get(t) if t else main)
    return main, spb


def test_drain_two_tabs_two_reads_two_writes(tmp_path, monkeypatch):
    _ready(tmp_path)
    main_rows = [[str(1000 + i), "Одобрена", "", ""] for i in range(150)]
    spb_rows = [[str(2000 + i), "Одобрена", "", ""] for i in range(150)]
    main, spb = _tabs(monkeypatch, main_rows, spb_rows)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await chat_tracking.bind_chat(None, CHAT, "Чат", "msk")
        await chat_tracking.bind_chat(None, CHAT - 1, "Чат СПб", "spb")
        for i in range(150):
            await _user(1000 + i, "msk")
            await _user(2000 + i, "spb")
        await db.upsert_chat_member(CHAT, 1000, "member", source="refresh")
        await db.upsert_chat_member(CHAT - 1, 2000, "left", source="refresh")
        async with db._connect() as conn:
            await conn.execute("DELETE FROM sheet_chat_queue")
            await conn.commit()
        await db.enqueue_sheet_chat_cells([1000 + i for i in range(150)] + [2000 + i for i in range(150)])
        await db.enqueue_sheet_chat_cells([1000, 2000])  # дубли — схлопнутся
        counts = await sheet_chat_sync.drain()
        return counts, await _queue()

    counts, rows = _run(go())
    assert counts == {"written": 300, "missing": 0, "failed": 0}
    assert main.batch_get_calls == 1 and spb.batch_get_calls == 1
    assert len(main.batch_update_calls) == 1 and len(spb.batch_update_calls) == 1
    assert len(main.batch_update_calls[0]) == 150
    assert main.rows[0][3] == "да" and main.rows[1][3] == "не проверено"
    assert spb.rows[0][3] == "нет"
    assert all(r[2] == "" for r in main.rows + spb.rows)  # «Пришёл» не тронут
    assert rows == []


def test_drain_missing_row_is_dropped(tmp_path, monkeypatch):
    _ready(tmp_path)
    _tabs(monkeypatch, [], [])

    async def go():
        await _user(7)
        await db.enqueue_sheet_chat_cells([7])
        counts = await sheet_chat_sync.drain()
        return counts, await _queue()

    counts, rows = _run(go())
    assert counts == {"written": 0, "missing": 1, "failed": 0}
    assert rows == []


def test_drain_sheet_without_column_drops_and_touches_nothing(tmp_path, monkeypatch):
    _ready(tmp_path)
    old_header = HEADER[:-1]
    main, _ = _tabs(monkeypatch, [["7", "Одобрена", "03.10 09:00"]], [], header=old_header)

    async def go():
        await _user(7)
        await db.enqueue_sheet_chat_cells([7])
        counts = await sheet_chat_sync.drain()
        return counts, await _queue()

    counts, rows = _run(go())
    assert counts["missing"] == 1
    assert main.batch_update_calls == []
    assert main.rows == [["7", "Одобрена", "03.10 09:00"]]
    assert rows == []


def test_drain_failure_keeps_events_with_backoff_and_redacts(tmp_path, monkeypatch):
    _ready(tmp_path)
    main, _ = _tabs(monkeypatch, [["7", "Одобрена", "", ""]], [])
    main.fail_update = gspread.exceptions.GSpreadException(
        "boom token=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )

    async def go():
        await _user(7)
        await db.enqueue_sheet_chat_cells([7])
        counts = await sheet_chat_sync.drain()
        return counts, await _queue()

    counts, rows = _run(go())
    assert counts == {"written": 0, "missing": 0, "failed": 1}
    assert len(rows) == 1 and rows[0]["attempts"] == 1
    assert rows[0]["next_try_at"] > rows[0]["created_at"]
    assert "AAAAAAAAAAAAAAAAAAAA" not in (rows[0]["last_error"] or "")


def test_drain_without_sheet_config_drops(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")

    async def go():
        await _user(7)
        await db.enqueue_sheet_chat_cells([7])
        counts = await sheet_chat_sync.drain()
        return counts, await _queue()

    counts, rows = _run(go())
    assert rows == []


def test_scheduler_registers_drain_job():
    import inspect

    import services.scheduler as scheduler

    src = inspect.getsource(scheduler)
    assert '"sheet_chat_drain"' in src
    assert hasattr(scheduler, "sheet_chat_drain_job")


def test_delete_map_covers_queue():
    assert ("sheet_chat_queue", "telegram_id", "checkin") in db.USER_PURGE_TABLES
