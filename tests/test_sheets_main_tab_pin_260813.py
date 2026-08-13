"""Quick task 260813: harden main Google Sheet tab resolution.

Real production incident — with GOOGLE_SHEET_TAB empty, _get_sheet() used to resolve the main
tab by RAW POSITION (sh.sheet1) on every cache-miss. A manager reordering tabs in the Google
Sheets UI silently redirected every registration write, and "♻️ Пересобрать таблицу"
(rebuild_main_sheet's sheet.clear()) then wiped whatever tab happened to be first.

Fix under test:
1. Position is used AT MOST ONCE — the resolved title is persisted (bot_settings) and every
   later resolution (including after _reset_sheet_cache() / a process restart) targets that
   SAME title by name. Reordering tabs afterwards can no longer redirect writes.
2. rebuild_main_sheet / dedupe_sheet_by_id (the two row-deleting/clearing paths) refuse to run
   and return REFUSED_UNPINNED_TAB until GOOGLE_SHEET_TAB is set explicitly; the admin UI shows
   an actionable message instead of the generic API-error text.
3. warn_if_tab_unconfigured() alerts admins once per process start when unconfigured.

Follows tests/test_sheets_phase5.py / tests/test_sheets_admin_alert.py conventions: plain
def test_*, asyncio.run(go()), monkeypatch, config.DB_PATH pointed at a tmp_path DB (bot_settings
needs db.init_db() to exist for the persistence tests). Mocks gspread entirely — no real
Google API calls.
"""
import asyncio

import gspread

from config import config
from database import db
from handlers import admin as admin_mod
import services.sheets as sheets


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _reset_module_state():
    """Every test starts from a clean slate: no cached worksheet, no alert bot, both one-shot
    warning flags cleared (module globals persist across tests otherwise)."""
    sheets._reset_sheet_cache()
    sheets.set_alert_bot(None)
    sheets._alert_bot_warned = False
    sheets._startup_tab_warning_sent = False


# ── Fake gspread plumbing (mirrors tests/test_sheets_phase5.py) ────────────────────────────

class _FakeWorksheet:
    def __init__(self, title):
        self.title = title


class _FakeSpreadsheet:
    """Ordered list of worksheets — .sheet1 is whichever is FIRST, mirroring gspread's real
    positional resolution. reorder() simulates an admin dragging a tab in the Sheets UI."""

    def __init__(self, titles):
        self._sheets = {t: _FakeWorksheet(t) for t in titles}
        self._order = list(titles)

    @property
    def sheet1(self):
        return self._sheets[self._order[0]]

    def worksheet(self, title):
        if title not in self._sheets:
            raise gspread.WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self._sheets[title] = ws
        self._order.append(title)
        return ws

    def reorder(self, new_first_title):
        self._order.remove(new_first_title)
        self._order.insert(0, new_first_title)


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


# ── Task 1: explicit GOOGLE_SHEET_TAB resolves by name, unaffected by position ─────────────

def test_get_sheet_explicit_tab_resolves_by_name(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_module_state()
    _patch_gspread_client(monkeypatch, ["Первая", "Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги бот")

    ws = sheets._get_sheet()
    assert ws.title == "Реги бот"


def test_get_sheet_explicit_tab_auto_creates_when_missing(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_module_state()
    _patch_gspread_client(monkeypatch, ["Первая"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Новая вкладка")

    ws = sheets._get_sheet()
    assert ws.title == "Новая вкладка"


# ── Task 1: empty setting resolves by position ONCE, then stays pinned by name ─────────────

def test_get_sheet_empty_tab_pins_after_first_resolution(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_module_state()
    asyncio.run(db.init_db())

    fake_ss = _patch_gspread_client(monkeypatch, ["Реги бот", "Party"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    first = sheets._get_sheet()
    assert first.title == "Реги бот"

    # Someone reorders tabs in the Sheets UI — "Party" is now first by position.
    fake_ss.reorder("Party")
    sheets._reset_sheet_cache()

    second = sheets._get_sheet()
    assert second.title == "Реги бот"  # still pinned — NOT the new sh.sheet1


def test_get_sheet_pin_survives_cache_reset_and_fresh_client(tmp_path, monkeypatch):
    """Simulates a process restart: _reset_sheet_cache() plus a brand-new fake spreadsheet
    object (as if gspread.service_account()/open_by_key() ran fresh), same DB_PATH. The pin
    must be read back from bot_settings, not re-derived from the new client's sheet1."""
    _use_tmp_db(tmp_path)
    _reset_module_state()
    asyncio.run(db.init_db())

    _patch_gspread_client(monkeypatch, ["Реги бот", "Party"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    first = sheets._get_sheet()
    assert first.title == "Реги бот"

    sheets._reset_sheet_cache()
    # Fresh spreadsheet object with a DIFFERENT tab first by position.
    _patch_gspread_client(monkeypatch, ["Party", "Реги бот"])
    second = sheets._get_sheet()
    assert second.title == "Реги бот"


def test_get_sheet_repin_when_pinned_tab_deleted(tmp_path, monkeypatch):
    """The pinned tab was renamed/deleted since the pin was set — there is no safe "same tab"
    target left, so _get_sheet() falls back to position again and re-pins."""
    _use_tmp_db(tmp_path)
    _reset_module_state()
    asyncio.run(db.init_db())

    _patch_gspread_client(monkeypatch, ["Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    first = sheets._get_sheet()
    assert first.title == "Реги бот"

    sheets._reset_sheet_cache()
    # The old pinned tab is gone; a new tab is first by position now.
    _patch_gspread_client(monkeypatch, ["Новая основная"])
    second = sheets._get_sheet()
    assert second.title == "Новая основная"


# ── Task 2: destructive ops refuse to run on a positionally-resolved tab ───────────────────

def test_rebuild_main_sheet_refused_when_tab_unpinned(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    async def go():
        return await sheets.rebuild_main_sheet(["A"], [[1]])

    assert asyncio.run(go()) == sheets.REFUSED_UNPINNED_TAB


def test_rebuild_main_sheet_allowed_when_tab_named(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги бот")

    def fake_rebuild_sync(headers, rows):
        return len(rows)

    monkeypatch.setattr(sheets, "_rebuild_main_sheet_sync", fake_rebuild_sync)

    async def go():
        return await sheets.rebuild_main_sheet(["A"], [[1], [2]])

    assert asyncio.run(go()) == 2


def test_dedupe_sheet_by_id_refused_when_tab_unpinned(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    async def go():
        return await sheets.dedupe_sheet_by_id()

    assert asyncio.run(go()) == sheets.REFUSED_UNPINNED_TAB


def test_dedupe_sheet_by_id_allowed_when_tab_named(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги бот")

    def fake_dedupe_sync():
        return 3

    monkeypatch.setattr(sheets, "_dedupe_sheet_sync", fake_dedupe_sync)

    async def go():
        return await sheets.dedupe_sheet_by_id()

    assert asyncio.run(go()) == 3


def test_rebuild_main_sheet_still_returns_minus_one_when_sheets_unconfigured(monkeypatch):
    """Unconfigured Sheets integration (no ID/credentials) keeps its OWN -1 signal, distinct
    from REFUSED_UNPINNED_TAB — the unconfigured check runs first."""
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    async def go():
        return await sheets.rebuild_main_sheet(["A"], [[1]])

    assert asyncio.run(go()) == -1


# ── Task 2: admin UI shows an actionable message, not the generic error text ───────────────

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class _FakeCallback:
    def __init__(self, user_id):
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


ADMIN_ID = 900813


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def test_rebuild_sheet_handler_shows_actionable_message_when_unpinned(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    async def fake_headers():
        return ["A"]

    async def fake_users():
        return []

    monkeypatch.setattr(admin_mod, "active_sheet_headers", fake_headers)
    monkeypatch.setattr(admin_mod, "get_all_users_dicts", fake_users)

    callback = _FakeCallback(ADMIN_ID)

    async def go():
        await admin_mod.rebuild_sheet(callback)

    asyncio.run(go())
    assert "GOOGLE_SHEET_TAB" in callback.message.text
    assert "⛔" in callback.message.text


def test_rebuild_sheet_handler_succeeds_when_named(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    _reset_module_state()

    async def fake_headers():
        return ["A"]

    async def fake_users():
        return []

    async def fake_rebuild(headers, rows):
        return 0

    monkeypatch.setattr(admin_mod, "active_sheet_headers", fake_headers)
    monkeypatch.setattr(admin_mod, "get_all_users_dicts", fake_users)
    monkeypatch.setattr(admin_mod, "rebuild_main_sheet", fake_rebuild)

    callback = _FakeCallback(ADMIN_ID)

    async def go():
        await admin_mod.rebuild_sheet(callback)

    asyncio.run(go())
    assert "GOOGLE_SHEET_TAB" not in callback.message.text
    assert "пересобрана" in callback.message.text.lower()


def test_dedupe_sheet_run_handler_shows_actionable_message_when_unpinned(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    callback = _FakeCallback(ADMIN_ID)

    async def go():
        await admin_mod.dedupe_sheet_run(callback)

    asyncio.run(go())
    assert "GOOGLE_SHEET_TAB" in callback.message.text
    assert "⛔" in callback.message.text


# ── Task 3: startup warning fires exactly once per process, reuses _send_admin_alert ───────

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_warn_if_tab_unconfigured_fires_once(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)

    async def go():
        await sheets.warn_if_tab_unconfigured()
        await sheets.warn_if_tab_unconfigured()  # second call must be a no-op

    asyncio.run(go())

    # Exactly ONE round of admin alerts (111, 222), not two.
    assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]


def test_warn_if_tab_unconfigured_noop_when_tab_set(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги бот")
    monkeypatch.setattr(config, "ADMIN_IDS", [111])

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)

    async def go():
        await sheets.warn_if_tab_unconfigured()

    asyncio.run(go())
    assert fake_bot.sent == []


def test_warn_if_tab_unconfigured_noop_when_sheets_integration_off(monkeypatch):
    _reset_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    monkeypatch.setattr(config, "ADMIN_IDS", [111])

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)

    async def go():
        await sheets.warn_if_tab_unconfigured()

    asyncio.run(go())
    assert fake_bot.sent == []
