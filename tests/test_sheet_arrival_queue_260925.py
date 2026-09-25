"""Нагрузочный прогон 25.09: «Пришёл» в Google-лист — через очередь `sheet_arrival_queue`.

- отметка (в т.ч. путь Mini App — `record_arrival` без bot) пишет только в базу и очередь,
  лист не вызывается;
- джоба (`services/sheet_arrival_sync.drain`): пачка по двум вкладкам -> ровно 2 чтения и
  2 batch_update, события удалены; дубли по делегату схлопываются; повтор безвреден;
- сбой batch_update -> события остались, attempts + 1, backoff, ошибка без секретов;
- снятие отметки -> ячейка пересчитана (пусто, если входов не осталось);
- CSV на 500 строк -> одно чтение на вкладку;
- делегата нет в листе -> событие снято;
- сторож: services/checkin.py, services/venue_log.py и miniapp/ не импортируют Google-листы."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import gspread
import pytest

from config import config
from database import db
import services.sheets as sheets
from services import sheet_arrival_sync, venue_log
from services.checkin import record_arrival
from tests.test_sheet_status_city_tab_260819 import _patch_fake_sheets, _setup_city_user, _use_tmp_db

ROOT = Path(__file__).resolve().parent.parent
HEADER = ["id", sheets.STATUS_HEADER, sheets.ARRIVED_HEADER]


class QueueFakeWorksheet:
    def __init__(self, title, rows=None, header=HEADER):
        self.title = title
        self.header = list(header)
        self.rows = rows or []
        self.batch_get_calls = 0
        self.batch_update_calls: list[list[dict]] = []
        self.fail_update: Exception | None = None

    def row_values(self, n):
        raise AssertionError("очередь читает шапку одним batch_get, не row_values")

    def col_values(self, n):
        raise AssertionError("очередь читает столбец id одним batch_get, не col_values")

    def update(self, *a, **k):
        raise AssertionError("очередь пишет одним batch_update")

    def batch_get(self, ranges):
        assert ranges == ["1:1", "A:A"]
        self.batch_get_calls += 1
        return [[list(self.header)], [[self.header[0]]] + [[r[0]] for r in self.rows]]

    def batch_update(self, updates, value_input_option=None):
        if self.fail_update is not None:
            raise self.fail_update
        self.batch_update_calls.append(updates)
        for u in updates:
            row, col = gspread.utils.a1_to_rowcol(u["range"])
            self.rows[row - 2][col - 1] = u["values"][0][0]


def _run(coro):
    return asyncio.run(coro)


async def _queue_rows() -> list[dict]:
    return await db.list_due_sheet_arrivals("9999-12-31 00:00:00", 10_000)


async def _user(tid):
    return await db.get_user(tid)


# ── отметка: только база + очередь ─────────────────────────────────────────────────────────

def test_miniapp_path_enqueues_and_never_touches_sheet(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    def boom(*a, **k):
        raise AssertionError("отметка не должна ходить в Google-лист")

    monkeypatch.setattr(sheets, "_get_sheet", boom)
    monkeypatch.setattr(sheets, "_get_named_sheet", boom)
    monkeypatch.setattr(sheets, "update_arrived_in_sheet", boom)
    monkeypatch.setattr(sheets, "write_arrivals_batch", boom)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")

    async def go():
        await _setup_city_user(501, "spb")
        # Mini App зовёт record_arrival без bot (miniapp/routers/checkin.py).
        result = await record_arrival(await _user(501), "entry", source="miniapp", by_staff_id=1)
        return result, await _queue_rows()

    result, rows = _run(go())
    assert result["status"] == "new"
    assert [(r["telegram_id"], r["action"], r["city"]) for r in rows] == [(501, db.SHEET_ARRIVAL_SET, "spb")]


def test_enqueue_is_fail_soft_without_table(tmp_path):
    """Mini App поднялся раньше миграции бота — таблицы нет, отметка не падает."""
    config.DB_PATH = str(tmp_path / "empty.db")
    _run(db.enqueue_sheet_arrival(1, db.SHEET_ARRIVAL_SET))  # не бросает


# ── джоба: пачка по вкладкам ───────────────────────────────────────────────────────────────

def _two_tabs(monkeypatch):
    main = QueueFakeWorksheet("main", rows=[["701", "Одобрена", ""], ["702", "Одобрена", ""]])
    spb = QueueFakeWorksheet("СПб", rows=[["801", "Одобрена", ""], ["802", "Одобрена", ""], ["803", "Одобрена", ""]])
    tabs = {"__main__": main, "СПб": spb}
    _patch_fake_sheets(monkeypatch, tabs)
    # Очередь открывает вкладку без автосоздания (`_open_named_or_main_sync`), не `_get_named_sheet`.
    monkeypatch.setattr(sheets, "_open_named_or_main_sync", lambda t: tabs.get(t) if t else main)
    return main, spb


async def _mark(tid, stamp):
    await db.record_checkin(tid, "entry", source="miniapp", scanned_at=stamp)
    await db.enqueue_sheet_arrival(tid, db.SHEET_ARRIVAL_SET)


def test_batch_two_tabs_two_reads_two_writes(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    main, spb = _two_tabs(monkeypatch)

    async def go():
        await _setup_city_user(701, None)
        for tid in (702,):
            await db.add_user({"telegram_id": tid, "event_city": None, "participant_type": "full",
                               "registration_date": "2026-08-19T00:00:00"})
        for tid in (801, 802, 803):
            await db.add_user({"telegram_id": tid, "event_city": "spb", "participant_type": "full",
                               "registration_date": "2026-08-19T00:00:00"})
        for i, tid in enumerate((701, 702, 801, 802, 803)):
            await _mark(tid, f"2026-10-03 09:0{i}:00")
        await db.enqueue_sheet_arrival(801, db.SHEET_ARRIVAL_SET)  # дубль — схлопнется
        counts = await sheet_arrival_sync.drain()
        return counts, await _queue_rows()

    counts, rows = _run(go())
    assert counts == {"written": 5, "missing": 0, "failed": 0}
    assert main.batch_get_calls == 1 and spb.batch_get_calls == 1
    assert len(main.batch_update_calls) == 1 and len(spb.batch_update_calls) == 1
    assert len(spb.batch_update_calls[0]) == 3  # дубль 801 — одна ячейка
    assert [r[2] for r in main.rows] == ["2026-10-03 09:00:00", "2026-10-03 09:01:00"]
    assert [r[2] for r in spb.rows] == ["2026-10-03 09:02:00", "2026-10-03 09:03:00", "2026-10-03 09:04:00"]
    assert rows == []


class _NoCreateSpreadsheet:
    def worksheet(self, title):
        raise gspread.WorksheetNotFound(title)

    def add_worksheet(self, *a, **k):
        raise AssertionError("очередь «Пришёл» не имеет права создавать вкладку")


def test_missing_city_tab_is_not_created_and_row_goes_to_main(tmp_path, monkeypatch):
    """Вкладку города переименовали/удалили: add_worksheet не зовётся, ячейка пишется на
    главном листе вторым проходом (инцидент 05.09)."""
    _use_tmp_db(tmp_path)
    main = QueueFakeWorksheet("main", rows=[["801", "Одобрена", ""]])
    _patch_fake_sheets(monkeypatch, {"__main__": main})
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda t: (_ for _ in ()).throw(
        AssertionError("_get_named_sheet создаёт вкладку на промахе — очереди нельзя")))
    monkeypatch.setattr(sheets, "_named_sheets", {})
    client = type("C", (), {"open_by_key": lambda self, key: _NoCreateSpreadsheet()})()
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename=None: client)

    async def go():
        await _setup_city_user(801, "spb")
        await _mark(801, "2026-10-03 09:00:00")
        counts = await sheet_arrival_sync.drain()
        return counts, await _queue_rows()

    counts, rows = _run(go())
    assert counts == {"written": 1, "missing": 0, "failed": 0}
    assert main.rows[0][2] == "2026-10-03 09:00:00"
    assert rows == []


def test_repeat_is_idempotent(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    main, _ = _two_tabs(monkeypatch)

    async def go():
        await _setup_city_user(701, None)
        await _mark(701, "2026-10-03 09:00:00")
        await sheet_arrival_sync.drain()
        snapshot = [list(r) for r in main.rows]
        await sheet_arrival_sync.drain()  # пустая очередь — ни одного похода в лист
        await db.enqueue_sheet_arrival(701, db.SHEET_ARRIVAL_SET)  # повтор события
        await sheet_arrival_sync.drain()
        return snapshot

    snapshot = _run(go())
    assert main.rows == snapshot
    assert main.batch_get_calls == 2


def test_batch_update_failure_keeps_events_with_backoff(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    main, _ = _two_tabs(monkeypatch)
    main.fail_update = RuntimeError("proxy dropped https://api.telegram.org/bot123456:" + "A" * 35)

    async def go():
        await _setup_city_user(701, None)
        await _mark(701, "2026-10-03 09:00:00")
        first = await sheet_arrival_sync.drain()
        rows1 = await _queue_rows()
        # Ещё не созрело — джоба событие не берёт.
        second = await sheet_arrival_sync.drain()
        return first, rows1, second

    first, rows, second = _run(go())
    assert first["failed"] == 1
    assert second == {"written": 0, "missing": 0, "failed": 0}
    assert len(rows) == 1
    row = rows[0]
    assert row["attempts"] == 1
    assert row["next_try_at"] > row["created_at"]
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in row["last_error"]
    assert main.rows[0][2] == ""  # не записано


def test_backoff_schedule():
    got = [sheet_arrival_sync.backoff_seconds(n) for n in range(1, 10)]
    assert got == [30, 60, 120, 240, 480, 960, 1800, 1800, 1800]


def test_failed_event_retried_after_backoff_and_removed(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    main, _ = _two_tabs(monkeypatch)
    main.fail_update = RuntimeError("503")

    async def go():
        await _setup_city_user(701, None)
        await _mark(701, "2026-10-03 09:00:00")
        await sheet_arrival_sync.drain()
        main.fail_update = None
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:  # «прошло 30 с»
            await conn.execute("UPDATE sheet_arrival_queue SET next_try_at = '2000-01-01 00:00:00'")
            await conn.commit()
        counts = await sheet_arrival_sync.drain()
        return counts, await _queue_rows()

    counts, rows = _run(go())
    assert counts["written"] == 1 and rows == []
    assert main.rows[0][2] == "2026-10-03 09:00:00"


def test_revoke_recomputes_cell_to_empty(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    main, _ = _two_tabs(monkeypatch)

    async def go():
        await _setup_city_user(701, None)
        await _mark(701, "2026-10-03 09:00:00")
        await sheet_arrival_sync.drain()
        written = main.rows[0][2]
        checkin_id = (await db.list_checkins_for_user(701))[0]["id"]
        await venue_log.revoke_mark(checkin_id, staff_id=1, staff_name="Менеджер")
        rows = await _queue_rows()
        await sheet_arrival_sync.drain()
        return written, rows

    written, rows = _run(go())
    assert written == "2026-10-03 09:00:00"
    assert [r["action"] for r in rows] == [db.SHEET_ARRIVAL_RECOMPUTE]
    assert main.rows[0][2] == ""


def test_missing_row_drops_event(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    main, _ = _two_tabs(monkeypatch)

    async def go():
        await _setup_city_user(999, None)  # в листе такой строки нет
        await _mark(999, "2026-10-03 09:00:00")
        counts = await sheet_arrival_sync.drain()
        return counts, await _queue_rows()

    counts, rows = _run(go())
    assert counts["missing"] == 1 and rows == []
    assert main.batch_update_calls == []


def test_csv_500_rows_one_read_per_tab(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    ids = list(range(10_000, 10_500))
    main = QueueFakeWorksheet("main", rows=[[str(t), "Одобрена", ""] for t in ids])
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    def boom(*a, **k):
        raise AssertionError("CSV не должен ходить в лист построчно")

    monkeypatch.setattr(sheets, "update_arrived_in_sheet", boom)

    async def go():
        await _setup_city_user(ids[0], None)
        for tid in ids[1:]:
            await db.add_user({"telegram_id": tid, "event_city": None, "participant_type": "full",
                               "registration_date": "2026-08-19T00:00:00"})
        for tid in ids:
            await record_arrival(await _user(tid), "entry", source="csv",
                                 scanned_at="2026-10-03 09:00:00")
        before = main.batch_get_calls
        counts = await sheet_arrival_sync.drain()
        return before, counts, await _queue_rows()

    before, counts, rows = _run(go())
    assert before == 0
    assert counts["written"] == 500 and rows == []
    assert main.batch_get_calls == 1 and len(main.batch_update_calls) == 1
    assert all(r[2] == "2026-10-03 09:00:00" for r in main.rows)


def test_queue_stats(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _setup_city_user(1, None)
        empty = await db.sheet_arrival_queue_stats()
        await db.enqueue_sheet_arrival(1, db.SHEET_ARRIVAL_SET)
        await db.enqueue_sheet_arrival(1, db.SHEET_ARRIVAL_SET)
        return empty, await db.sheet_arrival_queue_stats()

    empty, stats = _run(go())
    assert empty == (0, None)
    assert stats[0] == 2 and stats[1]


# ── сторож: отметка и Mini App без Google-листов ───────────────────────────────────────────

_FORBIDDEN = ("services.sheets", "gspread")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{a.name}" for a in node.names}
    return names


_GUARDED = [ROOT / "services" / "checkin.py", ROOT / "services" / "venue_log.py"] + sorted(
    (ROOT / "miniapp").rglob("*.py")
)


@pytest.mark.parametrize("path", _GUARDED, ids=lambda p: str(p.relative_to(ROOT)))
def test_checkin_and_miniapp_do_not_import_google_sheets(path):
    bad = {n for n in _imports(path) if any(n == f or n.startswith(f + ".") for f in _FORBIDDEN)}
    assert not bad, f"{path.relative_to(ROOT)} импортирует Google-листы: {sorted(bad)} — пишите в очередь"
