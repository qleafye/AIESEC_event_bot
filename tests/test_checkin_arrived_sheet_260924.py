"""Форум-ночь B2 (идея №17): время прихода в Google-таблицу — точечное обновление колонки
«Пришёл», тот же путь, что «Статус» (services.sheets.sheets.update_status_in_sheet). Покрывает:

- `services.sheets.sheets._arrived_col_index`/`update_arrived_in_sheet` — колонка находится по имени
  шапки (не по фиксированному индексу), пишется РОВНО одна ячейка, лист без этой колонки
  (собран до того, как её завели) — fail-soft False, без исключения.
- Named-tab-first, fallback на main — тот же приём, что `tests/test_sheet_status_city_tab_
  260819.py` для «Статус» (FakeWorksheet/_patch_fake_sheets переиспользованы оттуда).
- `services.forum.checkin.mark_arrived_in_sheet` — ставит событие в очередь ТОЛЬКО на `status == "new"`;
  `"duplicate"` не ставит ничего. Сам лист пишет джоба очереди (services/sheets/sheet_arrival_sync.py,
  подробно — tests/test_sheet_arrival_queue_260925.py), отметка лист не трогает."""
from __future__ import annotations

import asyncio

import gspread

from config import config
import services.sheets.sheets as sheets
from database import db
from services.sheets import sheet_arrival_sync
from services.forum.checkin import mark_arrived_in_sheet
from tests.test_sheet_status_city_tab_260819 import _patch_fake_sheets, _setup_city_user, _use_tmp_db


class ArrivedFakeWorksheet:
    """Как FakeWorksheet в test_sheet_status_city_tab_260819.py, но с настраиваемой шапкой —
    той тестовой таблице «Пришёл» не нужна была, этой нужна ИМЕННО она."""

    def __init__(self, title, header, rows=None):
        self.title = title
        self.header = header
        self.rows = rows or []
        self.update_calls: list[tuple[list[list], str]] = []

    def row_values(self, n):
        assert n == 1
        return list(self.header)

    def col_values(self, n):
        assert n == 1
        return [self.header[0]] + [r[0] for r in self.rows]

    def update(self, values, range_name, value_input_option=None):
        self.update_calls.append((values, range_name))
        row, col = gspread.utils.a1_to_rowcol(range_name)
        self.rows[row - 2][col - 1] = values[0][0]

    def batch_get(self, ranges):
        assert ranges == ["1:1", "A:A"]
        return [[list(self.header)], [[self.header[0]]] + [[r[0]] for r in self.rows]]

    def batch_update(self, updates, value_input_option=None):
        for u in updates:
            self.update_calls.append((u["values"], u["range"]))
            row, col = gspread.utils.a1_to_rowcol(u["range"])
            self.rows[row - 2][col - 1] = u["values"][0][0]


HEADER = ["id", sheets.STATUS_HEADER, sheets.ARRIVED_HEADER]


def test_arrived_col_index_finds_column_by_name():
    sheet = ArrivedFakeWorksheet("main", HEADER, rows=[])
    assert sheets._arrived_col_index(sheet) == 2


def test_arrived_col_index_missing_column_is_minus_one():
    sheet = ArrivedFakeWorksheet("main", ["id", sheets.STATUS_HEADER], rows=[])
    assert sheets._arrived_col_index(sheet) == -1


def test_update_arrived_writes_single_cell_on_main_sheet(tmp_path, monkeypatch):
    main = ArrivedFakeWorksheet("main", HEADER, rows=[["555", "Одобрена", "-"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        await _setup_city_user(555, None)  # цель по умолчанию -> main
        return await sheets.update_arrived_in_sheet(555, "2026-10-03 09:15:00")

    assert asyncio.run(go()) is True
    assert main.rows == [["555", "Одобрена", "03.10 09:15"]]
    assert main.update_calls == [([["03.10 09:15"]], "C2")]


def test_update_arrived_routes_to_city_tab_first(tmp_path, monkeypatch):
    main = ArrivedFakeWorksheet("main", HEADER, rows=[])
    spb = ArrivedFakeWorksheet("СПб", HEADER, rows=[["444", "Одобрена", "-"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await _setup_city_user(444, "spb")
        return await sheets.update_arrived_in_sheet(444, "2026-10-03 10:00:00")

    assert asyncio.run(go()) is True
    assert spb.rows == [["444", "Одобрена", "03.10 10:00"]]
    assert main.rows == []


def test_update_arrived_falls_back_to_main_when_row_only_on_main(tmp_path, monkeypatch):
    main = ArrivedFakeWorksheet("main", HEADER, rows=[["777", "Одобрена", "-"]])
    spb = ArrivedFakeWorksheet("СПб", HEADER, rows=[])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await _setup_city_user(777, "spb")
        return await sheets.update_arrived_in_sheet(777, "2026-10-03 11:00:00")

    assert asyncio.run(go()) is True
    assert main.rows == [["777", "Одобрена", "03.10 11:00"]]


def test_update_arrived_missing_column_returns_false_no_crash(tmp_path, monkeypatch):
    """Лист собран ДО того, как в схему добавили «Пришёл» (менеджер не жал «♻️ Пересобрать
    таблицу») — fail-soft False, отметка в БД уже сохранена вызывающим до этого вызова."""
    main = ArrivedFakeWorksheet("main", ["id", sheets.STATUS_HEADER], rows=[["555", "Одобрена"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        await _setup_city_user(555, None)
        return await sheets.update_arrived_in_sheet(555, "2026-10-03 09:15:00")

    assert asyncio.run(go()) is False
    assert main.rows == [["555", "Одобрена"]]  # не тронуто


def test_update_arrived_not_found_returns_false(tmp_path, monkeypatch):
    main = ArrivedFakeWorksheet("main", HEADER, rows=[])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        await _setup_city_user(999, None)
        return await sheets.update_arrived_in_sheet(999, "2026-10-03 09:15:00")

    assert asyncio.run(go()) is False


# ── services.forum.checkin.mark_arrived_in_sheet: только на 'new' ─────────────────────────────────

def test_mark_arrived_writes_on_new(tmp_path, monkeypatch):
    main = ArrivedFakeWorksheet("main", HEADER, rows=[["555", "Одобрена", "-"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        await _setup_city_user(555, None)
        await db.record_checkin(555, "entry", source="miniapp", scanned_at="2026-10-03 09:15:00")
        await mark_arrived_in_sheet(555, "new", "2026-10-03 09:15:00")
        untouched = [list(r) for r in main.rows]
        await sheet_arrival_sync.drain()
        return untouched

    assert asyncio.run(go()) == [["555", "Одобрена", "-"]]  # сама отметка лист не трогает
    assert main.rows == [["555", "Одобрена", "03.10 09:15"]]  # записала джоба очереди


def test_mark_arrived_skips_write_on_duplicate(tmp_path, monkeypatch):
    main = ArrivedFakeWorksheet("main", HEADER, rows=[["555", "Одобрена", "09:00"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        await _setup_city_user(555, None)
        await mark_arrived_in_sheet(555, "duplicate", "2026-10-03 09:15:00")
        return await db.sheet_arrival_queue_stats()

    assert asyncio.run(go())[0] == 0  # в очередь ничего
    assert main.rows == [["555", "Одобрена", "09:00"]]  # не переписано
    assert main.update_calls == []  # Sheets API не дёрнут вовсе
