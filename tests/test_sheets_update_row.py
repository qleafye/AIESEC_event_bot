"""Phase 21 (21-03, FORM-SYNC-04, D-16): contract for services/sheets.py::update_row_by_id —
point update of a delegate's ROW by telegram_id (col1 match), instead of the second
append_row that today duplicates a row on every re-registration/edit (RESEARCH Pitfall 4).

RED (this file, Task 1): written BEFORE the implementation exists — every test below currently
fails with AttributeError (`services.sheets` has no `update_row_by_id`). GREEN comes next
(Task 2), which adds `_update_row_by_id_sync`/`update_row_by_id` following the same skeleton as
`_update_status_in_row_range`/`_update_status_in_sheet_sync` (col1 scan, named-tab-first with
fallback to the main sheet, fail-soft False+warning when the row isn't found anywhere) plus
`append_to_sheet`'s RETRY_DELAYS retry loop + `_alert_admins_sheet_failure` after exhaustion.

Mock reused from tests/test_sheet_status_city_tab_260819.py (FakeWorksheet, _patch_fake_sheets)
per plan instruction — not copied. RecordingWorksheet below is a NEW subclass adding gspread's
`update(values=..., range_name=...)` (a whole-range write), which FakeWorksheet doesn't need for
the status-cell-only test it was written for.

pytest-asyncio unavailable in this env — async entry points driven via asyncio.run(), same
convention as the file above.
"""
import asyncio
import logging

import gspread

from config import config
import services.sheets as sheets
from tests.test_sheet_status_city_tab_260819 import FakeWorksheet, _patch_fake_sheets


class RecordingWorksheet(FakeWorksheet):
    """FakeWorksheet + gspread's `update(values=..., range_name=...)` — the ONE range-write
    update_row_by_id must use instead of update_cell (T-21-10: one row, one API call, no
    per-cell loop, no rebuild). `fail_times` simulates a gspread exception on the first N
    `update()` calls, to drive the retry/alert tests without waiting on real RETRY_DELAYS."""

    def __init__(self, title, rows=None, header=None, fail_times=0):
        super().__init__(title, rows=rows)
        if header is not None:
            self.header = header
        self.update_calls: list[tuple[list[list], str]] = []
        self._fail_times = fail_times
        self._fail_count = 0

    def update(self, values, range_name):
        if self._fail_count < self._fail_times:
            self._fail_count += 1
            raise RuntimeError("simulated gspread API failure")
        self.update_calls.append((values, range_name))
        row, _col = gspread.utils.a1_to_rowcol(range_name.split(":")[0])
        idx = row - 2  # rows list is 0-based, row 2 (first data row) -> rows[0]
        while len(self.rows) <= idx:
            self.rows.append([])
        self.rows[idx] = list(values[0])


# ── found on the named tab: exactly one range update, no update_cell, width unchanged ───────

def test_update_row_found_on_named_tab_single_range_update(monkeypatch):
    header = ["id", "Статус", "Детали"]
    named = RecordingWorksheet("СПб", rows=[["555", "Одобрена", "old details"]], header=header)
    main = RecordingWorksheet("main", rows=[], header=header)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": named})

    new_row = ["555", "Одобрена", "new details"]

    async def go():
        return await sheets.update_row_by_id("СПб", 555, new_row)

    assert asyncio.run(go()) is True
    assert named.update_calls == [([new_row], "A2:C2")]
    assert named.update_cell_calls == []
    assert len(new_row) == len(header)
    assert main.update_calls == []


# ── not found anywhere: fail-soft False + warning, no exception, no PII in the log ──────────

def test_update_row_not_found_anywhere_returns_false_and_warns(monkeypatch, caplog):
    header = ["id", "Статус", "Детали"]
    main = RecordingWorksheet("main", rows=[], header=header)
    spb = RecordingWorksheet("СПб", rows=[], header=header)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    secret_row = ["999", "Одобрена", "SecretPhoneNumber12345"]

    async def go():
        return await sheets.update_row_by_id("СПб", 999, secret_row)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(go())

    assert result is False
    assert any("999" in r.message and "not found" in r.message for r in caplog.records)
    assert not any("SecretPhoneNumber12345" in r.message for r in caplog.records)
    assert main.update_calls == []
    assert spb.update_calls == []


# ── found only on the main sheet (legacy row, predates tab pinning): fallback updates it ────

def test_update_row_found_only_on_main_sheet_fallback(monkeypatch):
    header = ["id", "Статус", "Детали"]
    main = RecordingWorksheet("main", rows=[["777", "Одобрена", "old"]], header=header)
    spb = RecordingWorksheet("СПб", rows=[], header=header)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    new_row = ["777", "Одобрена", "new"]

    async def go():
        return await sheets.update_row_by_id("СПб", 777, new_row)

    assert asyncio.run(go()) is True
    assert main.update_calls == [([new_row], "A2:C2")]
    assert spb.update_calls == []


# ── header row is never a match candidate, even if its col1 cell looks like an id ───────────

def test_update_row_header_not_treated_as_candidate(monkeypatch):
    header = ["42", "Статус", "Детали"]  # pathological: header's own col1 cell looks like an id
    main = RecordingWorksheet("main", rows=[], header=header)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        return await sheets.update_row_by_id(None, 42, ["42", "Одобрена", "x"])

    result = asyncio.run(go())

    assert result is False
    assert main.update_calls == []


# ── gspread exception on first attempt: retried via RETRY_DELAYS, then succeeds ─────────────

def test_update_row_retries_on_exception_then_succeeds(monkeypatch):
    header = ["id", "Статус", "Детали"]
    main = RecordingWorksheet("main", rows=[["555", "Одобрена", "old"]], header=header, fail_times=1)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    new_row = ["555", "Одобрена", "new"]

    async def go():
        return await sheets.update_row_by_id(None, 555, new_row)

    assert asyncio.run(go()) is True
    assert main.update_calls == [([new_row], "A2:C2")]
    assert sleeps == [sheets.RETRY_DELAYS[0]]


# ── retries exhausted: one admin alert, False, no partial write ─────────────────────────────

def test_update_row_alert_after_retries_exhausted(monkeypatch):
    header = ["id", "Статус", "Детали"]
    main = RecordingWorksheet("main", rows=[["555", "Одобрена", "old"]], header=header, fail_times=99)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    alert_calls: list[str] = []

    async def fake_alert(context):
        alert_calls.append(context)

    monkeypatch.setattr(sheets, "_alert_admins_sheet_failure", fake_alert)

    async def go():
        return await sheets.update_row_by_id(None, 555, ["555", "Одобрена", "new"])

    assert asyncio.run(go()) is False
    assert len(alert_calls) == 1
    assert "555" in alert_calls[0]
    assert main.update_calls == []


# ── no Sheets credentials configured: skip without raising ──────────────────────────────────

def test_update_row_skips_without_credentials(monkeypatch, caplog):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    async def go():
        return await sheets.update_row_by_id(None, 555, ["555", "Одобрена", "x"])

    result = asyncio.run(go())

    assert result is False
