"""Quick task 260825-ldi: «🔄 Синхронизация» (sync_sheet) used to always dozapisyvat missing
delegates into the MAIN tab, even when their city routes them to a named tab. Prod snapshot
25.08.2026: main tab («МСК») held 46 rows that belonged to СПб/Тюмень — the same delegates were
already correctly present on their own city tabs. Neighbouring «♻️ Пересобрать» (rebuild_sheet)
got the same fix 17.08 (see tests/test_rebuild_city_routing_260818.py); this is sync_sheet's
symmetric fix.

Two sections:
  * services/sheets: get_existing_named_sheet_ids / append_rows_to_named_sheet — fail-soft,
    batch primitives mirroring the main-tab equivalents but keyed by tab name.
  * админка: sync_sheet routes missing rows per city_row_tab, one try/except per tab, human
    per-tab report, byte-identical behaviour when the cities module is off.
"""
import asyncio
import logging

from config import config
import services.sheets as sheets
from handlers import admin_sections
from handlers import admin_sheets  # module-size split: sync_sheet moved out of admin_sheets.py
from tests.test_rebuild_confirm_260813_sdl import _FakeCallback, ADMIN_ID


# ── services/sheets: именованная вкладка ─────────────────────────────────────────────────────

class _FakeNamedSheet:
    def __init__(self, col1=None, raise_on_col_values=False, raise_on_append=False):
        self._col1 = col1 or []
        self.raise_on_col_values = raise_on_col_values
        self.raise_on_append = raise_on_append
        self.append_rows_calls: list[list] = []

    def col_values(self, n):
        assert n == 1
        if self.raise_on_col_values:
            raise RuntimeError("boom-read")
        return list(self._col1)

    def append_rows(self, rows):
        if self.raise_on_append:
            raise RuntimeError("boom-append")
        self.append_rows_calls.append(rows)


def _fake_creds(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")


def test_get_existing_named_sheet_ids_parses_column(monkeypatch):
    _fake_creds(monkeypatch)
    fake = _FakeNamedSheet(col1=["id", "3", "мусор", "5"])
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab_name: fake)

    result = asyncio.run(sheets.get_existing_named_sheet_ids("СПб"))

    assert result == {3, 5}


def test_get_existing_named_sheet_ids_fails_soft_and_resets_cache(monkeypatch, caplog):
    _fake_creds(monkeypatch)
    fake = _FakeNamedSheet(raise_on_col_values=True)
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab_name: fake)
    reset_calls = []
    monkeypatch.setattr(sheets, "_reset_named_sheet_cache", lambda tab_name: reset_calls.append(tab_name))

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(sheets.get_existing_named_sheet_ids("СПб"))

    assert result is None
    assert reset_calls == ["СПб"]
    assert any("СПб" in r.message for r in caplog.records)


def test_get_existing_named_sheet_ids_no_credentials_returns_none(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    result = asyncio.run(sheets.get_existing_named_sheet_ids("СПб"))

    assert result is None


def test_append_rows_to_named_sheet_empty_list_is_noop(monkeypatch):
    _fake_creds(monkeypatch)
    fake = _FakeNamedSheet()
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab_name: fake)

    result = asyncio.run(sheets.append_rows_to_named_sheet("СПб", []))

    assert result == 0
    assert fake.append_rows_calls == []


def test_append_rows_to_named_sheet_batches_in_one_call(monkeypatch):
    _fake_creds(monkeypatch)
    fake = _FakeNamedSheet()
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab_name: fake)

    result = asyncio.run(sheets.append_rows_to_named_sheet("СПб", [[1], [2]]))

    assert result == 2
    assert fake.append_rows_calls == [[[1], [2]]]


def test_append_rows_to_named_sheet_fails_soft(monkeypatch, caplog):
    _fake_creds(monkeypatch)
    fake = _FakeNamedSheet(raise_on_append=True)
    monkeypatch.setattr(sheets, "_get_named_sheet", lambda tab_name: fake)
    reset_calls = []
    monkeypatch.setattr(sheets, "_reset_named_sheet_cache", lambda tab_name: reset_calls.append(tab_name))

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(sheets.append_rows_to_named_sheet("СПб", [[1]]))

    assert result == -1
    assert reset_calls == ["СПб"]

    # missing credentials -> -1 too, no API call attempted
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")
    assert asyncio.run(sheets.append_rows_to_named_sheet("СПб", [[1]])) == -1


# ── админка: маршрутизация синхронизации ─────────────────────────────────────────────────────

def _users():
    return [
        {"telegram_id": 1, "event_city": "msk", "participant_type": "full"},
        {"telegram_id": 2, "event_city": None, "participant_type": "full"},
        {"telegram_id": 3, "event_city": "spb", "participant_type": "full"},
        {"telegram_id": 4, "event_city": "spb", "participant_type": "full"},
        {"telegram_id": 5, "event_city": "tyumen", "participant_type": "full"},
    ]


def _wire(monkeypatch, route, existing_main=None, existing_named=None, append_named_result=None):
    """existing_named: dict tab -> set|None (result of get_existing_named_sheet_ids)
    append_named_result: dict tab -> int (result of append_rows_to_named_sheet), default len(rows)."""
    existing_named = existing_named or {}
    append_named_result = append_named_result or {}
    calls = {
        "ensure_header": [],
        "ensure_named_header": [],
        "append_main": [],
        "append_named": [],
    }

    # Phase 25 (CITYQ-03): sync_sheet now resolves headers PER CODE via sheet_city_code
    # (main tab = code None, each city tab = its own code) instead of one shared list — see
    # the identical fixture note in tests/test_rebuild_city_routing_260818.py::_wire. Reusing
    # `route`'s own city mapping here (participant_type is irrelevant to it) keeps this a fully
    # mocked handler test — no real read of the dev sqlite file at data/forum.db.
    async def fake_headers(city_code=None):
        return ["ID"]

    async def fake_city_code(event_city):
        return event_city if (await route(event_city, None)) is not None else None

    async def fake_users():
        return _users()

    async def fake_ensure_header(headers):
        calls["ensure_header"].append(headers)

    async def fake_existing_ids():
        return existing_main if existing_main is not None else set()

    async def fake_append_main(rows):
        calls["append_main"].append(rows)
        return len(rows)

    async def fake_ensure_named_header(tab, headers):
        calls["ensure_named_header"].append(tab)

    async def fake_existing_named_ids(tab):
        if tab in existing_named:
            return existing_named[tab]
        return set()

    async def fake_append_named(tab, rows):
        calls["append_named"].append((tab, rows))
        if tab in append_named_result:
            return append_named_result[tab]
        return len(rows)

    async def fake_kb(admin_id, callback_data=None):
        return None

    monkeypatch.setattr(admin_sheets, "active_sheet_headers", fake_headers)
    monkeypatch.setattr(admin_sheets, "sheet_city_code", fake_city_code)
    monkeypatch.setattr(admin_sheets, "get_all_users_dicts", fake_users)
    monkeypatch.setattr(admin_sheets, "ensure_sheet_header", fake_ensure_header)
    monkeypatch.setattr(admin_sheets, "get_existing_sheet_ids", fake_existing_ids)
    monkeypatch.setattr(admin_sheets, "append_rows_to_sheet", fake_append_main)
    monkeypatch.setattr(admin_sheets, "ensure_named_sheet_header", fake_ensure_named_header)
    monkeypatch.setattr(admin_sheets, "get_existing_named_sheet_ids", fake_existing_named_ids)
    monkeypatch.setattr(admin_sheets, "append_rows_to_named_sheet", fake_append_named)
    # Ревью фазы 20: экран результата берёт клавиатуру раздела-владельца операции, а не корня,
    # поэтому шов для подмены переехал с admin_keyboard_for на op_return_keyboard. Хендлер
    # импортирует её лениво из модуля — патч атрибута модуля перехватывает вызов.
    monkeypatch.setattr(admin_sections, "op_return_keyboard", fake_kb)
    monkeypatch.setattr(admin_sheets, "_sheet_value_map", lambda u: {"ID": u["telegram_id"]})
    monkeypatch.setattr(admin_sheets, "city_row_tab", route)
    return calls


def test_sync_routes_missing_spb_delegate_to_spb_tab_not_main(monkeypatch):
    async def route(event_city, participant_type):
        return {"spb": "СПб", "tyumen": "Тюмень"}.get(event_city)

    calls = _wire(monkeypatch, route)
    cb = _FakeCallback(ADMIN_ID, "admin_sync_sheet")
    asyncio.run(admin_sheets.sync_sheet(cb))

    # main gets only msk (1) + NULL city (2); spb/tyumen never reach append_rows_to_sheet
    assert calls["append_main"] == [[[1], [2]]]
    named = dict(calls["append_named"])
    assert named["СПб"] == [[3], [4]]
    assert named["Тюмень"] == [[5]]
    assert "СПб" in cb.message.text
    assert "Тюмень" in cb.message.text


def test_sync_skips_already_present_delegate_and_skips_empty_tab_call(monkeypatch):
    async def route(event_city, participant_type):
        return {"spb": "СПб", "tyumen": "Тюмень"}.get(event_city)

    # both spb delegates (3, 4) already on their tab -> nothing missing there
    calls = _wire(monkeypatch, route, existing_named={"СПб": {3, 4}})
    cb = _FakeCallback(ADMIN_ID, "admin_sync_sheet")
    asyncio.run(admin_sheets.sync_sheet(cb))

    named_tabs = [tab for tab, _rows in calls["append_named"]]
    assert "СПб" not in named_tabs  # nothing missing -> append not called at all
    assert "Тюмень" in named_tabs


def test_sync_module_off_is_byte_identical_to_old_behaviour(monkeypatch):
    async def route(event_city, participant_type):
        return None  # cities module off / no per-city base -> everything to main

    calls = _wire(monkeypatch, route)
    cb = _FakeCallback(ADMIN_ID, "admin_sync_sheet")
    asyncio.run(admin_sheets.sync_sheet(cb))

    assert calls["append_main"] == [[[1], [2], [3], [4], [5]]]
    assert calls["append_named"] == []
    assert cb.message.text == (
        "✅ Синхронизация завершена!\n\n"
        "Добавлено записей: <b>5</b>"
    )
    assert "Основная вкладка" not in cb.message.text
    assert "Не удалось" not in cb.message.text


def test_sync_module_off_no_missing_rows_uses_old_empty_text(monkeypatch):
    async def route(event_city, participant_type):
        return None

    calls = _wire(monkeypatch, route, existing_main={1, 2, 3, 4, 5})
    cb = _FakeCallback(ADMIN_ID, "admin_sync_sheet")
    asyncio.run(admin_sheets.sync_sheet(cb))

    assert calls["append_main"] == []
    assert cb.message.text == "✅ Таблица синхронизирована, пропущенных записей нет."


def test_sync_one_tab_failure_does_not_cancel_the_rest(monkeypatch):
    async def route(event_city, participant_type):
        return {"spb": "СПб", "tyumen": "Тюмень"}.get(event_city)

    # СПб can't be read at all (get_existing_named_sheet_ids -> None) -> tab must be reported
    # as failed, but Тюмень and main still get their rows.
    calls = _wire(monkeypatch, route, existing_named={"СПб": None})
    cb = _FakeCallback(ADMIN_ID, "admin_sync_sheet")
    asyncio.run(admin_sheets.sync_sheet(cb))

    named_tabs = [tab for tab, _rows in calls["append_named"]]
    assert "СПб" not in named_tabs  # unreadable tab -> no blind append attempted
    assert "Тюмень" in named_tabs
    assert calls["append_main"] == [[[1], [2]]]
    text = cb.message.text
    assert "СПб" in text
    assert "Не удалось" in text or "не удалось" in text.lower()
