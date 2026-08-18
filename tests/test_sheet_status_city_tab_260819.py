"""Quick 260819-sst (owner-reported bug): Approve/Reject/«Одобрить все» used to write the
«Статус» column only on the MAIN worksheet (services/sheets.py::_get_sheet()) — a delegate whose
row actually lives on a city tab (СПб/Тюмень) or a short/party sub-tab never got their status
cell updated at all. Fixed by resolving the delegate's tab the same way the live append does
(handlers/registration.py::city_row_tab + _sheet_kind, replicated in services/sheets.py as
_resolve_status_tab / _status_sheet_kind — see that module's docstrings for why it's a
replication rather than an import), with a fallback to the main sheet for legacy rows.

pytest-asyncio is unavailable in this env — async helpers are driven via asyncio.run() and
config.DB_PATH is pointed at a tmp_path file, same convention as
tests/test_city_sheets_phase71.py / tests/test_sheets_phase5.py. gspread worksheets are faked
directly (dict tab-name -> FakeWorksheet), monkeypatching sheets._get_sheet/_get_named_sheet —
same level of mock as tests/test_sheets_phase5.py's _FakeClient/_FakeSpreadsheet, just simpler
since the status-update path never calls open_by_key/add_worksheet directly.
"""
import asyncio
import logging

import gspread

from config import config
from database import db
import services.sheets as sheets


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_status_city_tab.db")


class FakeWorksheet:
    """Minimal gspread worksheet stand-in: a «Статус» column at index 2 (matches
    STATUS_HEADER's position on a 2-column ["id", "Статус"] sheet), col1 = telegram_id rows."""

    def __init__(self, title, rows=None):
        self.title = title
        self.header = ["id", sheets.STATUS_HEADER]
        self.rows = rows or []  # list of [telegram_id_str, status_label]
        self.update_cell_calls: list[tuple[int, int, str]] = []
        self.batch_update_calls: list[list[dict]] = []

    def row_values(self, n):
        assert n == 1
        return list(self.header)

    def col_values(self, n):
        assert n == 1
        return [self.header[0]] + [r[0] for r in self.rows]

    def update_cell(self, row, col, value):
        self.update_cell_calls.append((row, col, value))
        self.rows[row - 2][col - 1] = value

    def batch_update(self, updates):
        self.batch_update_calls.append(updates)
        for u in updates:
            row, col = gspread.utils.a1_to_rowcol(u["range"])
            self.rows[row - 2][col - 1] = u["values"][0][0]


def _patch_fake_sheets(monkeypatch, tabs: dict):
    """tabs: dict tab_name -> FakeWorksheet, plus a mandatory "__main__" key returned by
    _get_sheet(). Also flips on the GOOGLE_SHEET_ID/CREDENTIALS guard both update_status_in_sheet
    and bulk_update_status_in_sheet check before doing any work."""
    monkeypatch.setattr(sheets, "_get_sheet", lambda: tabs["__main__"])
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab_name: tabs[tab_name])
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")


def _user_row(telegram_id, event_city, participant_type="full"):
    return {
        "telegram_id": telegram_id,
        "event_city": event_city,
        "participant_type": participant_type,
        "registration_date": "2026-08-19T00:00:00",
    }


async def _setup_city_user(telegram_id, event_city, participant_type="full"):
    await db.init_db()
    await db.set_setting("event_city_enabled", "on")
    await db.add_user(_user_row(telegram_id, event_city, participant_type))


# ── _status_sheet_kind: pure classification, mirrors handlers/registration.py::_sheet_kind ──

def test_status_sheet_kind_table():
    assert sheets._status_sheet_kind(None) == "main"
    assert sheets._status_sheet_kind("full") == "main"
    assert sheets._status_sheet_kind("short") == "short"
    assert sheets._status_sheet_kind("party_overnight") == "party"
    assert sheets._status_sheet_kind("party_noovernight") == "party"


# ── update_status_in_sheet: single-id routing ────────────────────────────────────────────────

def test_update_status_routes_to_spb_tab_not_main(tmp_path, monkeypatch):
    main = FakeWorksheet("main", rows=[])
    spb = FakeWorksheet("СПб", rows=[["555", "Новая"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await _setup_city_user(555, "spb")
        return await sheets.update_status_in_sheet(555, "Одобрена")

    assert asyncio.run(go()) is True
    assert spb.rows == [["555", "Одобрена"]]
    assert main.rows == []
    assert main.update_cell_calls == []


def test_update_status_routes_to_city_short_subtab(tmp_path, monkeypatch):
    """Track suffix must be honoured too, not just the city base — a short-track delegate on a
    non-default city lands on "<base> Акция", not the city's main tab."""
    main = FakeWorksheet("main", rows=[])
    short_tab = FakeWorksheet("СПб Акция", rows=[["444", "Новая"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб Акция": short_tab})

    async def go():
        await _setup_city_user(444, "spb", participant_type="short")
        return await sheets.update_status_in_sheet(444, "Одобрена")

    assert asyncio.run(go()) is True
    assert short_tab.rows == [["444", "Одобрена"]]
    assert main.rows == []


def test_update_status_falls_back_to_main_when_row_only_on_main(tmp_path, monkeypatch):
    """Legacy row: delegate's event_city resolves to a city tab today, but the row itself was
    written to the main sheet before per-city routing existed (or before event_city was set on
    this record). update_status_in_sheet must still find and update it."""
    main = FakeWorksheet("main", rows=[["777", "Новая"]])
    spb = FakeWorksheet("СПб", rows=[])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await _setup_city_user(777, "spb")
        return await sheets.update_status_in_sheet(777, "Одобрена")

    assert asyncio.run(go()) is True
    assert main.rows == [["777", "Одобрена"]]
    assert spb.rows == []


def test_update_status_not_found_anywhere_warns_and_returns_false(tmp_path, monkeypatch, caplog):
    main = FakeWorksheet("main", rows=[])
    spb = FakeWorksheet("СПб", rows=[])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await _setup_city_user(999, "spb")
        return await sheets.update_status_in_sheet(999, "Отклонена")

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(go())

    assert result is False
    assert any(
        "999" in record.message and "not found" in record.message
        for record in caplog.records
    )


def test_update_status_moscow_user_regression_uses_main_directly(tmp_path, monkeypatch):
    """Regression guard: a Moscow/default-city delegate must keep resolving straight to the
    main sheet (city_row_tab returns None for it) — byte-identical to pre-fix behaviour."""
    main = FakeWorksheet("main", rows=[["111", "Новая"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main})

    async def go():
        await _setup_city_user(111, None)
        return await sheets.update_status_in_sheet(111, "Одобрена")

    assert asyncio.run(go()) is True
    assert main.rows == [["111", "Одобрена"]]


# ── bulk_update_status_in_sheet: grouping by resolved tab ───────────────────────────────────

def test_bulk_update_groups_ids_by_tab(tmp_path, monkeypatch):
    main = FakeWorksheet("main", rows=[["111", "Новая"]])
    spb = FakeWorksheet("СПб", rows=[["222", "Новая"]])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.add_user(_user_row(111, None))
        await db.add_user(_user_row(222, "spb"))
        return await sheets.bulk_update_status_in_sheet({"111": "Одобрена", "222": "Одобрена"})

    total = asyncio.run(go())
    assert total == 2
    assert main.rows == [["111", "Одобрена"]]
    assert spb.rows == [["222", "Одобрена"]]
    # exactly one batch_update per tab — no cross-tab scan/write duplication
    assert len(main.batch_update_calls) == 1
    assert len(spb.batch_update_calls) == 1


def test_bulk_update_falls_back_to_main_for_unresolved_city_row(tmp_path, monkeypatch):
    main = FakeWorksheet("main", rows=[["333", "Новая"]])
    spb = FakeWorksheet("СПб", rows=[])  # tab exists, row isn't there (legacy)
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.add_user(_user_row(333, "spb"))
        return await sheets.bulk_update_status_in_sheet({"333": "Отклонена"})

    total = asyncio.run(go())
    assert total == 1
    assert main.rows == [["333", "Отклонена"]]
    assert spb.batch_update_calls == []


def test_bulk_update_not_found_anywhere_warns_and_excludes_from_count(tmp_path, monkeypatch, caplog):
    main = FakeWorksheet("main", rows=[])
    spb = FakeWorksheet("СПб", rows=[])
    _use_tmp_db(tmp_path)
    _patch_fake_sheets(monkeypatch, {"__main__": main, "СПб": spb})

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.add_user(_user_row(888, "spb"))
        return await sheets.bulk_update_status_in_sheet({"888": "Отклонена"})

    with caplog.at_level(logging.WARNING):
        total = asyncio.run(go())

    assert total == 0
    assert any("888" in record.message for record in caplog.records)
