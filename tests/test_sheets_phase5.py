"""Phase 5 Plan 6 (Party Sheet Routing) tests.

pytest-asyncio is unavailable in this env, so async helpers are driven via asyncio.run()
and config.DB_PATH/config.GOOGLE_SHEET_ID are pointed at test-safe values — same convention
as tests/test_db_phase5.py / tests/test_registration_phase5.py.
"""
import asyncio

from config import config
from database import db
import services.sheets as sheets


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _clear_sheet_id(monkeypatch):
    """Credential-absent path: the primary testable path per the plan's <behavior> bullets."""
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")


# ── Task 1: append_to_named_sheet / ensure_named_sheet_header fail-soft when unconfigured ──

def test_append_to_named_sheet_noop_when_sheet_id_unset(monkeypatch):
    _clear_sheet_id(monkeypatch)

    async def go():
        await sheets.append_to_named_sheet("Party", [123, "Test"])

    asyncio.run(go())  # must not raise


def test_ensure_named_sheet_header_noop_when_sheet_id_unset(monkeypatch):
    _clear_sheet_id(monkeypatch)

    async def go():
        await sheets.ensure_named_sheet_header("Party", ["A", "B"])

    asyncio.run(go())  # must not raise


def test_append_to_named_sheet_swallows_raising_sync_call(monkeypatch):
    """A raising sync append must never propagate out of the async wrapper (fail-soft, D-11/D-12
    threat T-05-06-02). Credentials are present so the sync path is actually exercised; the
    retry loop's sleeps are patched to no-ops so the test stays fast."""
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")

    def boom(tab_name, data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_named_sheet_sync", boom)

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(sheets.asyncio, "sleep", fast_sleep)

    async def go():
        await sheets.append_to_named_sheet("Party", [123, "Test"])

    asyncio.run(go())  # must not raise despite every retry attempt failing


# ── Task 1: _get_named_sheet cache — same tab returns same object, different tabs distinct ──

class _FakeWorksheet:
    def __init__(self, title):
        self.title = title


class _FakeSpreadsheet:
    def __init__(self):
        self._sheets = {}
        self.opened = []

    def worksheet(self, title):
        if title not in self._sheets:
            import gspread
            raise gspread.WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self._sheets[title] = ws
        return ws


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread_client(monkeypatch):
    fake_ss = _FakeSpreadsheet()
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename: _FakeClient(fake_ss))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    return fake_ss


def test_get_named_sheet_returns_same_object_for_repeated_calls(monkeypatch):
    sheets._named_sheets.clear()
    _patch_gspread_client(monkeypatch)

    first = sheets._get_named_sheet("Party")
    second = sheets._get_named_sheet("Party")
    assert first is second


def test_get_named_sheet_distinct_entries_for_different_tabs(monkeypatch):
    sheets._named_sheets.clear()
    _patch_gspread_client(monkeypatch)

    party = sheets._get_named_sheet("Party")
    other = sheets._get_named_sheet("Отобранные")
    assert party is not other
    assert party.title == "Party"
    assert other.title == "Отобранные"


def test_reset_named_sheet_cache_removes_only_named_entry(monkeypatch):
    sheets._named_sheets.clear()
    _patch_gspread_client(monkeypatch)

    sheets._get_named_sheet("Party")
    sheets._get_named_sheet("Отобранные")
    assert "Party" in sheets._named_sheets
    assert "Отобранные" in sheets._named_sheets

    sheets._reset_named_sheet_cache("Party")

    assert "Party" not in sheets._named_sheets
    assert "Отобранные" in sheets._named_sheets
