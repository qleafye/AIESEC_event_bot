"""Quick task 260813: harden main Google Sheet tab resolution.

Real production incident — with GOOGLE_SHEET_TAB empty, _get_sheet() used to resolve the main
tab by RAW POSITION (sh.sheet1) on every cache-miss. A manager reordering tabs in the Google
Sheets UI silently redirected every registration write, and "♻️ Пересобрать таблицу"
(rebuild_main_sheet's sheet.clear()) then wiped whatever tab happened to be first.

Fix under test:
1. Position is NEVER used (tightened 2026-08-14 after the production audit found «STATISTICS»,
   a manager-maintained formulas tab, sitting first on the live spreadsheet). An explicit
   GOOGLE_SHEET_TAB resolves by name; a legacy pin in bot_settings still resolves by name for
   installs that have one; anything else raises instead of guessing.
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
    positional resolution. Kept so the tests can prove position is IGNORED: each case puts a
    decoy tab first and asserts it is never the one written to."""

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


# ── Task 1 (revised 2026-08-14): unconfigured main tab REFUSES; it never guesses ───────────
#
# The original fix let position decide once and then pinned that title. The production audit
# of 2026-08-14 killed that compromise: the first tab of the live spreadsheet is «STATISTICS»,
# a manager-maintained formulas sheet, so the single permitted guess would have pinned and then
# filled the worst possible target. Refusing costs at most one sheet row — the DB is the source
# of truth and «🔄 Синхронизация таблицы» replays it — so refusal is strictly the cheaper error.


def _seed_pin(title):
    """Write a legacy pin straight into bot_settings. Nothing in the code creates pins any
    more, so a test that needs one has to plant it — which is exactly the real-world case:
    an install that pinned a title back when the old behaviour was live."""
    import sqlite3

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (sheets._PINNED_MAIN_TAB_SETTING_KEY, title),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_sheet_empty_tab_refuses_instead_of_resolving_by_position(tmp_path, monkeypatch):
    """No GOOGLE_SHEET_TAB and no legacy pin: raise rather than write into whatever tab is
    first. The refusal must also leave NO pin behind — a guess must not become permanent."""
    _use_tmp_db(tmp_path)
    _reset_module_state()
    asyncio.run(db.init_db())

    _patch_gspread_client(monkeypatch, ["STATISTICS", "Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    try:
        sheets._get_sheet()
        assert False, "expected _get_sheet() to refuse an unconfigured main tab"
    except RuntimeError as e:
        assert "GOOGLE_SHEET_TAB" in str(e)

    assert sheets._load_pinned_tab_title() is None


def test_get_sheet_legacy_pin_still_wins_over_position(tmp_path, monkeypatch):
    """Back-compat: an install that pinned a title under the old behaviour keeps resolving to
    that title by name, across a cache reset and a fresh client, no matter what is first."""
    _use_tmp_db(tmp_path)
    _reset_module_state()
    asyncio.run(db.init_db())
    _seed_pin("Реги бот")

    _patch_gspread_client(monkeypatch, ["Party", "Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    assert sheets._get_sheet().title == "Реги бот"

    sheets._reset_sheet_cache()
    # Fresh spreadsheet object, yet another tab first by position.
    _patch_gspread_client(monkeypatch, ["STATISTICS", "Реги бот", "Party"])
    assert sheets._get_sheet().title == "Реги бот"


def test_get_sheet_refuses_when_pinned_tab_deleted(tmp_path, monkeypatch):
    """The pinned tab was renamed/deleted: there is no safe "same tab" target left, and the
    replacement is a human decision — refuse instead of re-guessing by position."""
    _use_tmp_db(tmp_path)
    _reset_module_state()
    asyncio.run(db.init_db())
    _seed_pin("Реги бот")

    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")
    _patch_gspread_client(monkeypatch, ["Новая основная"])

    try:
        sheets._get_sheet()
        assert False, "expected _get_sheet() to refuse after the pinned tab disappeared"
    except RuntimeError as e:
        assert "GOOGLE_SHEET_TAB" in str(e)

    # The stale pin is left untouched — re-pointing it is the admin's call, not a silent repin.
    assert sheets._load_pinned_tab_title() == "Реги бот"


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
