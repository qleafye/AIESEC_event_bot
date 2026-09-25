"""Phase 21 (21-03, FORM-SYNC-04, D-16): contract for services/sheets.py::update_row_by_id —
point update of a delegate's ROW by telegram_id (col1 match), instead of the second
append_row that today duplicates a row on every re-registration/edit (RESEARCH Pitfall 4).

Координатор 25.09 (ветка sheets-update-row-fix): fixture rewritten to mock at the
`gspread.service_account`/`open_by_key` level (`_FakeSpreadsheet`/`_FakeClient`/`_FakeWorksheet`,
imported from `tests/test_sheets_update_row_safe_260926.py`) instead of monkeypatching
`sheets._get_sheet`/`_get_named_sheet` directly — the implementation no longer resolves a
named tab through `_get_named_sheet` (which auto-creates on a miss, exactly the bug fixed on
that branch) and needs the real `sh.worksheets()` listing for its cross-tab search, so the old
function-level mock can no longer stand in for it. Test BEHAVIOR (what's asserted) is unchanged
except where noted — this file's job is the "found directly" / "retry" / "no credentials"
contract; the new safety branches (cross-tab search, ambiguous refusal, never-creates) live in
the sibling file above.

pytest-asyncio unavailable in this env — async entry points driven via asyncio.run(), same
convention as the file above.
"""
import asyncio
import logging

from config import config
import services.sheets as sheets
from tests.test_sheets_update_row_safe_260926 import (
    _FakeWorksheet,
    _patch_gspread,
    _reset_sheets_state,
)


def setup_function(_):
    _reset_sheets_state()


def teardown_function(_):
    _reset_sheets_state()


# ── found on the named tab: exactly one range update, no update_cell, width unchanged ───────

def test_update_row_found_on_named_tab_single_range_update(monkeypatch):
    header = ["id", "Статус", "Детали"]
    named = _FakeWorksheet("СПб", header, rows=[["555", "Одобрена", "old details"]])
    main = _FakeWorksheet("main", header, rows=[])
    _patch_gspread(monkeypatch, {"main": main, "СПб": named}, main_title="main")

    new_row = ["555", "Одобрена", "new details"]

    async def go():
        return await sheets.update_row_by_id("СПб", 555, new_row)

    assert asyncio.run(go()) is True
    assert named.update_calls == [([new_row], "A2:C2")]
    assert len(new_row) == len(header)
    assert main.update_calls == []


# ── not found anywhere: fail-soft False + warning, no exception, no PII in the log ──────────

def test_update_row_not_found_anywhere_returns_false_and_warns(monkeypatch, caplog):
    header = ["id", "Статус", "Детали"]
    main = _FakeWorksheet("main", header, rows=[])
    spb = _FakeWorksheet("СПб", header, rows=[])
    _patch_gspread(monkeypatch, {"main": main, "СПб": spb}, main_title="main")

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


# ── found only on the main sheet (same shape, legacy row predating tab routing): the
# header-gated cross-tab search finds it — координатор 25.09: this USED to exercise the OLD
# blind "named tab miss -> always also try main sheet" fallback (bug: it wrote to main even
# when main's header didn't match the target's). Now it exercises the SAFE replacement — main
# is the single HEADER-COMPATIBLE match found by scanning every real tab — same outcome for
# this same-shape case, different (guarded) code path. ─────────────────────────────────────

def test_update_row_found_only_on_main_sheet_via_compatible_cross_search(monkeypatch):
    header = ["id", "Статус", "Детали"]
    main = _FakeWorksheet("main", header, rows=[["777", "Одобрена", "old"]])
    spb = _FakeWorksheet("СПб", header, rows=[])
    _patch_gspread(monkeypatch, {"main": main, "СПб": spb}, main_title="main")

    new_row = ["777", "Одобрена", "new"]

    async def go():
        return await sheets.update_row_by_id("СПб", 777, new_row)

    assert asyncio.run(go()) is True
    assert main.update_calls == [([new_row], "A2:C2")]
    assert spb.update_calls == []


# ── header row is never a match candidate, even if its col1 cell looks like an id ───────────

def test_update_row_header_not_treated_as_candidate(monkeypatch):
    header = ["42", "Статус", "Детали"]  # pathological: header's own col1 cell looks like an id
    main = _FakeWorksheet("main", header, rows=[])
    _patch_gspread(monkeypatch, {"main": main}, main_title="main")

    async def go():
        return await sheets.update_row_by_id(None, 42, ["42", "Одобрена", "x"])

    result = asyncio.run(go())

    assert result is False
    assert main.update_calls == []


# ── gspread exception on first attempt: retried via RETRY_DELAYS, then succeeds ─────────────

def test_update_row_retries_on_exception_then_succeeds(monkeypatch):
    class _FlakyWorksheet(_FakeWorksheet):
        def __init__(self, *a, fail_times=0, **kw):
            super().__init__(*a, **kw)
            self._fail_times = fail_times
            self._fail_count = 0

        def update(self, values, range_name, value_input_option=None):
            if self._fail_count < self._fail_times:
                self._fail_count += 1
                raise RuntimeError("simulated gspread API failure")
            return super().update(values, range_name, value_input_option=value_input_option)

    header = ["id", "Статус", "Детали"]
    main = _FlakyWorksheet("main", header, rows=[["555", "Одобрена", "old"]], fail_times=1)
    _patch_gspread(monkeypatch, {"main": main}, main_title="main")

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
    class _FlakyWorksheet(_FakeWorksheet):
        def __init__(self, *a, fail_times=0, **kw):
            super().__init__(*a, **kw)
            self._fail_times = fail_times
            self._fail_count = 0

        def update(self, values, range_name, value_input_option=None):
            if self._fail_count < self._fail_times:
                self._fail_count += 1
                raise RuntimeError("simulated gspread API failure")
            return super().update(values, range_name, value_input_option=value_input_option)

    header = ["id", "Статус", "Детали"]
    main = _FlakyWorksheet("main", header, rows=[["555", "Одобрена", "old"]], fail_times=99)
    _patch_gspread(monkeypatch, {"main": main}, main_title="main")

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


# ── Phase 31 (31-06, D-26): пометка автоотказа в «Детали» — та же вкладка, та же ширина ─────

def test_update_row_auto_reject_note_in_details_same_width_same_tab(monkeypatch):
    header = ["id", "Статус", "ФИО", "Детали"]
    named = _FakeWorksheet("СПб", header, rows=[["555", "На модерации", "Иван Иванов", "-"]])
    main = _FakeWorksheet("main", header, rows=[])
    _patch_gspread(monkeypatch, {"main": main, "СПб": named}, main_title="main")

    new_row = ["555", "Отклонена", "Иван Иванов", "🤖 Автоотказ 20.09 (правило: Курс закрыт)"]

    async def go():
        return await sheets.update_row_by_id("СПб", 555, new_row)

    assert asyncio.run(go()) is True
    assert named.update_calls == [([new_row], "A2:D2")]
    assert len(new_row) == len(header)  # число колонок не изменилось
    assert main.update_calls == []  # та же вкладка, не главный лист


# ── no Sheets credentials configured: skip without raising ──────────────────────────────────

def test_update_row_skips_without_credentials(monkeypatch, caplog):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    async def go():
        return await sheets.update_row_by_id(None, 555, ["555", "Одобрена", "x"])

    result = asyncio.run(go())

    assert result is False
