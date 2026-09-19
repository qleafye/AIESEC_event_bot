"""Quick 260919-mlu: переименование вкладок Google-таблицы вместо их сиротства.

Прод-инцидент (память проекта sheet-tab-rename-trap): смена ключа-имени вкладки в настройках
не переименовывает реальный лист — `services/sheets.py::_get_sheet`/`_get_named_sheet` на
`WorksheetNotFound` заводят НОВУЮ пустую вкладку, а старая с данными остаётся сиротой. Три
поколения «Незавершённые» и два поколения «Гейма» на проде — тому доказательство.

Файл растёт по задачам квика (образец — 06-01-style структура, один файл на весь квик):
- Task 1: примитивы `services/sheets.py` (`list_worksheet_titles`, `rename_worksheet`);
- Task 2: `settings_ops.py` (`normalize_tab_prefix`, `current_tab_titles`, `plan_prefix_renames`);
- Task 3: развилка при смене одного ключа-имени (`handlers/admin_sheet_tabs.py`);
- Task 4: массовые кнопки «Добавить/Убрать префикс».

Идиомы — как в tests/test_sheets_main_tab_pin_260813.py / test_sheets_admin_alert.py:
plain `def test_*`, `asyncio.run(go())`, monkeypatch, никаких реальных вызовов Google.
"""
import asyncio

import gspread

from config import config
from database import db
import services.sheets as sheets


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_sheet_tab_rename_260919.db")


def _reset_sheets_module_state():
    """Mirrors tests/test_sheets_main_tab_pin_260813.py::_reset_module_state."""
    sheets._reset_sheet_cache()
    sheets.set_alert_bot(None)
    sheets._alert_bot_warned = False
    sheets._startup_tab_warning_sent = False


# ── Fake gspread plumbing ────────────────────────────────────────────────────────────────

class _FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self._rows = rows if rows is not None else [["header"]]
        self.update_title_calls = []

    def get_all_values(self):
        return self._rows

    def update_title(self, new_title):
        self.update_title_calls.append(new_title)
        self.title = new_title


class _FakeSpreadsheet:
    """List (not dict) of worksheets, mirroring gspread's real `worksheets()` ordering — a
    dict keyed by title would go stale the moment `update_title` renames one in place."""

    def __init__(self, titles):
        self._list = [_FakeWorksheet(t) for t in titles]

    def worksheet(self, title):
        for ws in self._list:
            if ws.title == title:
                return ws
        raise gspread.WorksheetNotFound(title)

    def worksheets(self):
        return list(self._list)

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self._list.append(ws)
        return ws


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread_client(monkeypatch, titles):
    fake_ss = _FakeSpreadsheet(titles)
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename: _FakeClient(fake_ss))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    return fake_ss


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: services/sheets.py primitives
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_rename_worksheet_ok(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    fake_ss = _patch_gspread_client(monkeypatch, ["Незавершённые", "Краткая"])

    result = asyncio.run(sheets.rename_worksheet("Незавершённые", "NOT FILLED REGS"))

    assert result == "ok"
    ws = fake_ss.worksheet("NOT FILLED REGS")
    assert ws.title == "NOT FILLED REGS"
    assert ws.get_all_values() == [["header"]]  # данные на месте, лист тот же объект


def test_rename_worksheet_not_found(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    fake_ss = _patch_gspread_client(monkeypatch, ["Краткая"])

    result = asyncio.run(sheets.rename_worksheet("Нет такой", "Новое имя"))

    assert result == "not_found"
    assert all(not ws.update_title_calls for ws in fake_ss.worksheets())


def test_rename_worksheet_duplicate(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    fake_ss = _patch_gspread_client(monkeypatch, ["Незавершённые", "NOT FILLED REGS"])

    result = asyncio.run(sheets.rename_worksheet("Незавершённые", "NOT FILLED REGS"))

    assert result == "duplicate"
    titles = {ws.title for ws in fake_ss.worksheets()}
    assert titles == {"Незавершённые", "NOT FILLED REGS"}  # ни один title не тронут


def test_rename_worksheet_error_alerts_admins_and_does_not_raise(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(old, new):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_rename_worksheet_sync", boom)
    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)

    result = asyncio.run(sheets.rename_worksheet("Старое", "Новое"))

    assert result == "error"
    assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]


def test_rename_worksheet_ok_resets_caches(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    _patch_gspread_client(monkeypatch, ["Гейма", "Гейма бот"])

    # Предзаполняем кэши вручную — как если бы бот только что писал в обе вкладки.
    sheets._sheet = object()
    sheets._named_sheets["Гейма"] = object()
    sheets._named_sheets["Гейма бот"] = object()
    sheets._header_checked_tabs.add("Гейма")
    sheets._header_checked_tabs.add("Гейма бот")

    result = asyncio.run(sheets.rename_worksheet("Гейма", "Гейма бот 2"))

    assert result == "ok"
    assert sheets._sheet is None
    assert "Гейма" not in sheets._named_sheets
    assert "Гейма" not in sheets._header_checked_tabs
    assert "Гейма бот 2" not in sheets._named_sheets
    assert "Гейма бот 2" not in sheets._header_checked_tabs


def test_rename_worksheet_skipped_on_same_or_empty_names(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    _patch_gspread_client(monkeypatch, ["Краткая"])

    assert asyncio.run(sheets.rename_worksheet("Краткая", "Краткая")) == "skipped"
    assert asyncio.run(sheets.rename_worksheet("", "Новое")) == "skipped"
    assert asyncio.run(sheets.rename_worksheet("Краткая", "")) == "skipped"


def test_list_worksheet_titles_unconfigured_returns_none(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    assert asyncio.run(sheets.list_worksheet_titles()) is None


def test_list_worksheet_titles_returns_order(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    _patch_gspread_client(monkeypatch, ["Первая", "Реги бот", "Краткая"])

    titles = asyncio.run(sheets.list_worksheet_titles())

    assert titles == ["Первая", "Реги бот", "Краткая"]
