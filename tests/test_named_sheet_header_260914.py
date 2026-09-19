"""Инцидент 13.09 («СПб Акция» без строки заголовков) — заголовок именованной вкладки должен
писаться при ПЕРВОМ аппенде, не только из админки.

pytest-asyncio в окружении нет — каждый async-тест ведётся через asyncio.run(), тот же приём,
что в tests/test_sheets_phase5.py / tests/test_city_sheets_phase71.py.
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg
import services.sheets as sheets


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_named_sheet_header.db")


class _FakeWorksheet:
    """Минимальный двойник gspread.Worksheet: считает вызовы, эмулирует строку 1."""

    def __init__(self, first_row=None, col_count=26):
        self.col_count = col_count
        self.calls: list[tuple] = []
        self._row1 = list(first_row) if first_row else []
        self.data_rows: list[list] = []

    def col_values(self, col):
        self.calls.append(("col_values", col))
        if col == 1 and self._row1:
            return [self._row1[0]]
        return []

    def row_values(self, row):
        self.calls.append(("row_values", row))
        return list(self._row1) if row == 1 else []

    def append_row(self, row, value_input_option=None):
        self.calls.append(("append_row", list(row)))
        if not self._row1:
            self._row1 = list(row)
        else:
            self.data_rows.append(list(row))

    def insert_row(self, row, index, value_input_option=None):
        self.calls.append(("insert_row", list(row), index))
        self._row1 = list(row)

    def update(self, values=None, range_name=None, value_input_option=None):
        self.calls.append(("update", values, range_name))

    def add_cols(self, n):
        self.calls.append(("add_cols", n))
        self.col_count += n


def _patch_named_sheet(monkeypatch, fakes: dict):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab: fakes[tab])


def _reset_caches():
    sheets._header_checked_tabs.clear()
    sheets._named_sheets.clear()


# ── (а) пустая вкладка: заголовок пишется ДО строки данных ──────────────────────────────────

def test_append_writes_header_before_data_on_empty_tab(monkeypatch):
    _reset_caches()
    fake = _FakeWorksheet()
    _patch_named_sheet(monkeypatch, {"Party": fake})

    async def go():
        await sheets.append_to_named_sheet("Party", [1, "x"], headers=["ID", "Name"])

    asyncio.run(go())

    append_calls = [c for c in fake.calls if c[0] == "append_row"]
    assert len(append_calls) == 2
    assert append_calls[0][1] == ["ID", "Name"]
    assert append_calls[1][1] == [1, "x"]
    assert "Party" in sheets._header_checked_tabs


# ── (б) первая ячейка строки 1 — число: insert_row(headers, 1), затем аппенд строки ──────────

def test_append_inserts_header_when_row1_is_data(monkeypatch):
    _reset_caches()
    fake = _FakeWorksheet(first_row=["123", "existing"])
    _patch_named_sheet(monkeypatch, {"Party": fake})

    async def go():
        await sheets.append_to_named_sheet("Party", [1, "x"], headers=["ID", "Name"])

    asyncio.run(go())

    insert_calls = [c for c in fake.calls if c[0] == "insert_row"]
    assert insert_calls == [("insert_row", ["ID", "Name"], 1)]
    append_calls = [c for c in fake.calls if c[0] == "append_row"]
    assert append_calls == [("append_row", [1, "x"])]
    assert "Party" in sheets._header_checked_tabs


# ── (в) повторный аппенд в ту же вкладку в том же процессе не ходит в col_values снова ───────

def test_second_append_same_process_skips_header_check(monkeypatch):
    _reset_caches()
    fake = _FakeWorksheet()
    _patch_named_sheet(monkeypatch, {"Party": fake})

    async def go():
        await sheets.append_to_named_sheet("Party", [1, "x"], headers=["ID", "Name"])
        fake.calls.clear()
        await sheets.append_to_named_sheet("Party", [2, "y"], headers=["ID", "Name"])

    asyncio.run(go())

    assert not any(c[0] == "col_values" for c in fake.calls)
    assert ("append_row", [2, "y"]) in fake.calls


# ── (г) исключение внутри ensure не мешает аппенду строки, отметки нет ──────────────────────

def test_header_check_exception_does_not_block_append(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")

    def boom(tab_name, headers):
        raise RuntimeError("simulated ensure-header failure")

    monkeypatch.setattr(sheets, "_ensure_named_header_sync", boom)

    appended = []
    monkeypatch.setattr(
        sheets, "_append_to_named_sheet_sync",
        lambda tab, data: appended.append((tab, data)),
    )

    async def go():
        await sheets.append_to_named_sheet("Party", [1, "x"], headers=["ID", "Name"])

    asyncio.run(go())

    assert appended == [("Party", [1, "x"])]
    assert "Party" not in sheets._header_checked_tabs


# ── (д) после _reset_named_sheet_cache проверка заголовка выполняется снова ─────────────────

def test_reset_cache_forces_header_recheck(monkeypatch):
    _reset_caches()
    fake = _FakeWorksheet()
    _patch_named_sheet(monkeypatch, {"Party": fake})

    async def go():
        await sheets.append_to_named_sheet("Party", [1, "x"], headers=["ID", "Name"])
        sheets._reset_named_sheet_cache("Party")
        fake.calls.clear()
        await sheets.append_to_named_sheet("Party", [2, "y"], headers=["ID", "Name"])

    asyncio.run(go())

    assert any(c[0] == "col_values" for c in fake.calls)


# ── (е) headers=None — ни одного обращения к col_values, только аппенд (прежнее поведение) ──

def test_headers_none_skips_header_check_entirely(monkeypatch):
    _reset_caches()
    fake = _FakeWorksheet()
    _patch_named_sheet(monkeypatch, {"Party": fake})

    async def go():
        await sheets.append_to_named_sheet("Party", [1, "x"])

    asyncio.run(go())

    assert not any(c[0] == "col_values" for c in fake.calls)
    assert fake.calls == [("append_row", [1, "x"])]
    assert "Party" not in sheets._header_checked_tabs


# ── handlers.registration._sheet_headers_fn: тот же порядок, что _sheet_dispatch ────────────

def test_sheet_headers_fn_matches_sheet_dispatch_order():
    assert reg._sheet_headers_fn("party_overnight") is reg.party_sheet_headers
    assert reg._sheet_headers_fn("party_noovernight") is reg.party_sheet_headers
    assert reg._sheet_headers_fn("short") is reg.short_sheet_headers
    assert reg._sheet_headers_fn(None) is reg.active_sheet_headers
    assert reg._sheet_headers_fn("full") is reg.active_sheet_headers
