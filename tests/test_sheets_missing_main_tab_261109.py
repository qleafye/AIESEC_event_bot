"""Пропавшая главная вкладка не создаётся пустой.

Имя задано (настройка или GOOGLE_SHEET_TAB), а листа с таким названием в таблице нет —
раньше _get_sheet звал add_worksheet, и новые строки уезжали в пустую вкладку. Теперь
запись этого вызова не идёт, пустой лист не появляется, суперадминам одно сообщение
в час на это имя.
"""
import asyncio
import sqlite3

import pytest

import gspread

from config import config
import services.sheets.sheets as sheets
from tests._dbtpl import fast_init_db


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from services.sheets import sheet_target

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "missing_main.db"))
    fast_init_db()
    sheet_target.invalidate()
    sheets._reset_sheet_cache()
    sheets._missing_main_tab_alert_at.clear()
    sheets.set_alert_bot(None)
    sheets._alert_bot_warned = False
    yield
    sheets.set_alert_bot(None)
    sheets._missing_main_tab_alert_at.clear()
    sheets._reset_sheet_cache()
    sheet_target.invalidate()


class _FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.spreadsheet = None
        self.appended = []

    def append_row(self, data, value_input_option=None):
        self.appended.append(data)


class _FakeSpreadsheet:
    def __init__(self, titles):
        self._sheets = {t: _FakeWorksheet(t) for t in titles}
        for ws in self._sheets.values():
            ws.spreadsheet = self
        self.add_worksheet_calls = []

    def worksheet(self, title):
        if title not in self._sheets:
            raise gspread.WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.add_worksheet_calls.append(title)
        raise AssertionError(f"add_worksheet({title!r}) must not be called")

    def worksheets(self):
        return list(self._sheets.values())


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch(monkeypatch, titles):
    fake = _FakeSpreadsheet(titles)
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename: _FakeClient(fake))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    return fake


def _seed_main_tab(title):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("main_sheet_tab", title),
        )
        conn.commit()
    finally:
        conn.close()


def _capture_alerts(monkeypatch):
    alerts = []

    async def fake_alert(text):
        alerts.append(text)

    monkeypatch.setattr(sheets, "_send_admin_alert", fake_alert)
    return alerts


@pytest.mark.parametrize("via", ["setting", "env"])
def test_missing_configured_main_tab_does_not_add_worksheet(monkeypatch, via):
    fake = _patch(monkeypatch, ["Первая"])
    if via == "setting":
        _seed_main_tab("Старое имя")
        # Ступень 2 знает существующий лист — промах ступени 1 не должен на него свалиться.
        monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Первая")
    else:
        monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Старое имя")

    with pytest.raises(sheets.MainTabMissing) as exc:
        sheets._get_sheet()

    assert exc.value.tab == "Старое имя"
    assert fake.add_worksheet_calls == []
    assert "Старое имя" not in fake._sheets
    assert sheets._sheet is None


def test_existing_main_tab_opens_without_alert(monkeypatch):
    fake = _patch(monkeypatch, ["STATISTICS", "Реги бот"])
    _seed_main_tab("Реги бот")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    alerts = _capture_alerts(monkeypatch)

    async def go():
        sheets.set_alert_bot(object())
        ws = sheets._get_sheet()
        await asyncio.sleep(0)
        return ws

    ws = asyncio.run(go())
    assert ws.title == "Реги бот"
    assert fake.add_worksheet_calls == []
    assert alerts == []


def test_missing_main_tab_alert_text_and_hourly_throttle(monkeypatch):
    import time as time_mod

    fake = _patch(monkeypatch, ["Первая"])
    _seed_main_tab("Старое имя")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    alerts = _capture_alerts(monkeypatch)
    # Аптайм меньше часа: первый алерт обязан уйти, повтор того же имени — нет.
    monkeypatch.setattr(time_mod, "monotonic", lambda: 5.0)

    async def go():
        sheets.set_alert_bot(object())
        with pytest.raises(sheets.MainTabMissing):
            sheets._get_sheet()
        await asyncio.sleep(0)
        sheets._reset_sheet_cache()
        with pytest.raises(sheets.MainTabMissing):
            sheets._get_sheet()
        await asyncio.sleep(0)

    asyncio.run(go())

    assert fake.add_worksheet_calls == []
    assert len(alerts) == 1
    text = alerts[0]
    assert "«Старое имя»" in text
    assert "не найдена" in text
    assert "не уходят" in text
    assert "📄 Вкладки таблицы" in text
    assert "Google" in text
    assert "main_sheet_tab" not in text
    assert "GOOGLE_SHEET_TAB" not in text


def test_append_to_sheet_missing_main_tab_stays_fail_soft(monkeypatch):
    fake = _patch(monkeypatch, ["Первая"])
    _seed_main_tab("Старое имя")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    alerts = _capture_alerts(monkeypatch)

    async def fast_sleep(_delay):
        return None

    monkeypatch.setattr(sheets.asyncio, "sleep", fast_sleep)

    async def go():
        sheets.set_alert_bot(object())
        await sheets.append_to_sheet([7, "Иван"])
        await asyncio.sleep(0)

    asyncio.run(go())
    assert fake.add_worksheet_calls == []
    assert "Старое имя" not in fake._sheets
    assert len(alerts) == 1
    assert "попыток" not in alerts[0]


def test_open_named_or_main_explicit_miss_still_returns_none(monkeypatch):
    fake = _patch(monkeypatch, ["Первая"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    assert sheets._open_named_or_main_sync("Нет такой") is None
    assert fake.add_worksheet_calls == []


def test_open_named_or_main_none_does_not_create_missing_main(monkeypatch):
    fake = _patch(monkeypatch, ["Первая"])
    _seed_main_tab("Старое имя")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    with pytest.raises(sheets.MainTabMissing):
        sheets._open_named_or_main_sync(None)
    assert fake.add_worksheet_calls == []
