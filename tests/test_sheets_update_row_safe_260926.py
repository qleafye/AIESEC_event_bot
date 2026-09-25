"""Координатор 25.09 (fix, ветка sheets-update-row-fix): services/sheets.py::update_row_by_id
раньше на промахе целевой именованной вкладки ВСЕГДА откатывался на ГЛАВНЫЙ лист и писал туда
чужой строкой без проверки формы (памятка standalone-script-sheet-traps находка 2 /
lost-applications-260914 находка 2 — перенос города между вкладками тихо перезаписывал
Московскую строку колонками другого города). Этот файл покрывает НОВОЕ поведение
(_update_row_by_id_sync): целевая вкладка никогда не создаётся (то была отдельная дыра — старый
код звал `_get_named_sheet`, который делает `add_worksheet` на промахе), промах на целевой ищется
по ВСЕМ реально существующим вкладкам с СОВМЕСТИМОЙ (точное совпадение) шапкой, 2+ совпадения —
отказ без записи + алерт, а не угадывание.

Фейковый spreadsheet — тот же уровень мока, что `tests/test_city_move_260925.py`
(`_FakeSpreadsheet`/`_FakeClient`, `add_worksheet` бросает AssertionError — сам факт вызова уже
падение теста), `_FakeWorksheet` добавляет `row_values`/`update` поверх (нужны для шапки и
записи диапазона), как `RecordingWorksheet` в `tests/test_sheets_update_row.py`.

pytest-asyncio недоступна в этом окружении — async-хелперы через `asyncio.run()`, тот же приём,
что в соседних файлах этого модуля."""
import asyncio

import gspread
import pytest

from config import config
import services.sheets as sheets


class _FakeWorksheet:
    def __init__(self, title, header, rows=None):
        self.title = title
        self.header = list(header)
        self.rows = [list(r) for r in (rows or [])]  # data rows only, no header row
        self.update_calls: list[tuple[list[list], str]] = []
        # Never populated — production no longer calls gspread's update_cell (hardcodes
        # USER_ENTERED, no RAW override, находка 08-sheets-dashboard); kept only so sibling
        # files' `X.update_cell_calls == []` assertions (tests/test_sheets_last_row_wins_260913.py)
        # keep asserting exactly that: the method is never used.
        self.update_cell_calls: list[tuple[int, int, str]] = []

    def row_values(self, n):
        assert n == 1
        return list(self.header)

    def col_values(self, n):
        assert n == 1
        return [self.header[0]] + [str(r[0]) for r in self.rows]

    def update(self, values, range_name, value_input_option=None):
        self.update_calls.append((values, range_name))
        row, _col = gspread.utils.a1_to_rowcol(range_name.split(":")[0])
        idx = row - 2  # data rows are 0-based in self.rows, row 2 (first data row) -> rows[0]
        while len(self.rows) <= idx:
            self.rows.append([])
        self.rows[idx] = list(values[0])


class _FakeSpreadsheet:
    def __init__(self, worksheets: dict):
        self._by_title = dict(worksheets)
        self.add_worksheet_calls: list[str] = []

    def worksheet(self, title):
        if title not in self._by_title:
            raise gspread.WorksheetNotFound(title)
        return self._by_title[title]

    def worksheets(self):
        return list(self._by_title.values())

    def add_worksheet(self, title, rows, cols):
        # Сама попытка создать вкладку — уже провал теста (памятка standalone-script-sheet-traps
        # находка 1): _update_row_by_id_sync обязана только ЧИТАТЬ реальный список вкладок.
        self.add_worksheet_calls.append(title)
        raise AssertionError(f"add_worksheet({title!r}) must never be called by update_row_by_id")


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread(monkeypatch, worksheets: dict, main_title: str | None = None):
    """`worksheets`: dict title -> _FakeWorksheet, everything that really exists on the
    spreadsheet. `main_title` (optional): which entry `_get_sheet()` (tab_name=None route)
    should resolve to — stubbed directly, bypassing `_get_sheet`'s own multi-stage resolution
    logic (bot_settings.main_sheet_tab / GOOGLE_SHEET_TAB / pinned title), which isn't what this
    file is testing."""
    fake_ss = _FakeSpreadsheet(worksheets)
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename: _FakeClient(fake_ss))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    if main_title is not None:
        monkeypatch.setattr(sheets, "_get_sheet", lambda: worksheets[main_title])
    return fake_ss


def _reset_sheets_state():
    sheets._reset_sheet_cache()
    sheets._named_sheets.clear()
    sheets._header_checked_tabs.clear()
    sheets._row_ambiguous_alert_at.clear()


def _run(coro):
    return asyncio.run(coro)


HEADER = ["id", "Статус", "Детали"]
OTHER_HEADER = ["id", "Статус", "ФИО", "Детали"]  # другой трек/город — другой состав колонок


@pytest.fixture(autouse=True)
def _reset():
    _reset_sheets_state()
    yield
    _reset_sheets_state()


# ── branch 1: found directly on the target tab — no cross-search, no add_worksheet ──────────

def test_update_row_found_directly_on_target_no_cross_search(monkeypatch):
    target = _FakeWorksheet("Тюмень", HEADER, rows=[["555", "Одобрена", "old"]])
    main = _FakeWorksheet("main", HEADER, rows=[])
    fake_ss = _patch_gspread(monkeypatch, {"Тюмень": target, "main": main}, main_title="main")

    new_row = ["555", "Одобрена", "new"]
    result = _run(sheets.update_row_by_id("Тюмень", 555, new_row))

    assert result is True
    assert target.update_calls == [([new_row], "A2:C2")]
    assert main.update_calls == []
    assert fake_ss.add_worksheet_calls == []


# ── branch 2: not on target → exactly one OTHER tab has a compatible header AND the id ──────
# — updates THERE; a differently-shaped tab that also happens to contain the id is left alone.

def test_update_row_cross_tab_single_compatible_match_updates_there_mismatched_header_untouched(monkeypatch):
    target = _FakeWorksheet("Тюмень", HEADER, rows=[])  # freshly created, row not migrated yet
    main = _FakeWorksheet("main", HEADER, rows=[["777", "Одобрена", "old"]])  # same shape, has it
    wrong_shape = _FakeWorksheet("СПб Акция", OTHER_HEADER, rows=[["777", "Одобрена", "Иван", "x"]])
    fake_ss = _patch_gspread(
        monkeypatch, {"Тюмень": target, "main": main, "СПб Акция": wrong_shape}, main_title="main",
    )

    new_row = ["777", "Одобрена", "new"]
    result = _run(sheets.update_row_by_id("Тюмень", 777, new_row))

    assert result is True
    assert main.update_calls == [([new_row], "A2:C2")]
    assert target.update_calls == []
    assert wrong_shape.update_calls == []  # другая шапка — не кандидат, даже с совпавшим id
    assert fake_ss.add_worksheet_calls == []


# ── branch 3: found on 2+ header-compatible tabs — refuse, write nothing, alert once ────────

def test_update_row_ambiguous_multiple_compatible_tabs_writes_nothing_and_alerts(monkeypatch):
    target = _FakeWorksheet("Тюмень", HEADER, rows=[])
    main = _FakeWorksheet("main", HEADER, rows=[["888", "Одобрена", "a"]])
    spb = _FakeWorksheet("СПб", HEADER, rows=[["888", "Одобрена", "b"]])  # та же шапка — тоже кандидат
    fake_ss = _patch_gspread(
        monkeypatch, {"Тюмень": target, "main": main, "СПб": spb}, main_title="main",
    )
    alerts: list[str] = []

    async def fake_alert(text):
        alerts.append(text)

    monkeypatch.setattr(sheets, "_send_admin_alert", fake_alert)

    result = _run(sheets.update_row_by_id("Тюмень", 888, ["888", "Одобрена", "new"]))

    assert result is True  # «нечего дописывать» — вызывающий НЕ должен делать append
    assert target.update_calls == []
    assert main.update_calls == []
    assert spb.update_calls == []
    assert fake_ss.add_worksheet_calls == []
    assert len(alerts) == 1
    assert "888" in alerts[0]


def test_update_row_ambiguous_alert_throttled_within_cooldown(monkeypatch):
    target = _FakeWorksheet("Тюмень", HEADER, rows=[])
    main = _FakeWorksheet("main", HEADER, rows=[["888", "Одобрена", "a"]])
    spb = _FakeWorksheet("СПб", HEADER, rows=[["888", "Одобрена", "b"]])
    _patch_gspread(monkeypatch, {"Тюмень": target, "main": main, "СПб": spb}, main_title="main")
    alerts: list[str] = []

    async def fake_alert(text):
        alerts.append(text)

    monkeypatch.setattr(sheets, "_send_admin_alert", fake_alert)

    _run(sheets.update_row_by_id("Тюмень", 888, ["888", "Одобрена", "new1"]))
    _run(sheets.update_row_by_id("Тюмень", 888, ["888", "Одобрена", "new2"]))

    assert len(alerts) == 1  # второй вызов в пределах часа — тихо, без повтора


# ── branch 4: not found anywhere — safe False, caller does the append, nothing written ──────

def test_update_row_not_found_anywhere_returns_false_nothing_written(monkeypatch):
    target = _FakeWorksheet("Тюмень", HEADER, rows=[])
    main = _FakeWorksheet("main", HEADER, rows=[])  # same shape, but no matching id either
    wrong_shape = _FakeWorksheet("СПб Акция", OTHER_HEADER, rows=[["999", "Одобрена", "Иван", "x"]])
    fake_ss = _patch_gspread(
        monkeypatch, {"Тюмень": target, "main": main, "СПб Акция": wrong_shape}, main_title="main",
    )

    result = _run(sheets.update_row_by_id("Тюмень", 999, ["999", "Одобрена", "new"]))

    assert result is False
    assert target.update_calls == []
    assert main.update_calls == []
    assert wrong_shape.update_calls == []
    assert fake_ss.add_worksheet_calls == []


# ── target tab doesn't exist yet at all: never auto-created, never touches add_worksheet ────

def test_update_row_missing_target_tab_never_creates_it(monkeypatch):
    main = _FakeWorksheet("main", HEADER, rows=[])
    fake_ss = _patch_gspread(monkeypatch, {"main": main}, main_title="main")  # "Казань" doesn't exist

    result = _run(sheets.update_row_by_id("Казань", 123, ["123", "Одобрена", "x"]))

    assert result is False
    assert fake_ss.add_worksheet_calls == []
    assert main.update_calls == []


# ── missing target tab + expected_header: cross-search still finds the row elsewhere ────────

def test_update_row_missing_target_tab_with_expected_header_finds_compatible_row_elsewhere(monkeypatch):
    main = _FakeWorksheet("main", HEADER, rows=[["777", "Одобрена", "old"]])
    fake_ss = _patch_gspread(monkeypatch, {"main": main}, main_title="main")  # "Казань" doesn't exist

    new_row = ["777", "Одобрена", "new"]
    result = _run(
        sheets.update_row_by_id("Казань", 777, new_row, expected_header=list(HEADER)),
    )

    assert result is True
    assert main.update_calls == [([new_row], "A2:C2")]
    assert fake_ss.add_worksheet_calls == []


# ── missing target tab, no expected_header: no reference to compare against — safe False ────

def test_update_row_missing_target_tab_without_expected_header_skips_cross_search(monkeypatch):
    main = _FakeWorksheet("main", HEADER, rows=[["777", "Одобрена", "old"]])
    fake_ss = _patch_gspread(monkeypatch, {"main": main}, main_title="main")

    result = _run(sheets.update_row_by_id("Казань", 777, ["777", "Одобрена", "new"]))

    assert result is False  # без expected_header сравнивать шапку не с чем — не гадаем
    assert main.update_calls == []
    assert fake_ss.add_worksheet_calls == []


# ── a broken candidate tab during cross-search is skipped, not fatal ────────────────────────

def test_update_row_cross_search_skips_broken_candidate_tab(monkeypatch):
    class _BrokenWorksheet(_FakeWorksheet):
        def row_values(self, n):
            raise RuntimeError("simulated transient API error")

    target = _FakeWorksheet("Тюмень", HEADER, rows=[])
    broken = _BrokenWorksheet("Сломанная", HEADER, rows=[["777", "Одобрена", "x"]])
    main = _FakeWorksheet("main", HEADER, rows=[["777", "Одобрена", "old"]])
    fake_ss = _patch_gspread(
        monkeypatch, {"Тюмень": target, "Сломанная": broken, "main": main}, main_title="main",
    )

    new_row = ["777", "Одобрена", "new"]
    result = _run(sheets.update_row_by_id("Тюмень", 777, new_row))

    assert result is True
    assert main.update_calls == [([new_row], "A2:C2")]
    assert fake_ss.add_worksheet_calls == []


# ── network/API failure during the direct-target write still retries via RETRY_DELAYS ───────

def test_update_row_direct_target_exception_retries_then_succeeds(monkeypatch):
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

    target = _FlakyWorksheet("Тюмень", HEADER, rows=[["555", "Одобрена", "old"]], fail_times=1)
    _patch_gspread(monkeypatch, {"Тюмень": target}, main_title=None)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    new_row = ["555", "Одобрена", "new"]
    result = _run(sheets.update_row_by_id("Тюмень", 555, new_row))

    assert result is True
    assert target.update_calls == [([new_row], "A2:C2")]
    assert sleeps == [sheets.RETRY_DELAYS[0]]
