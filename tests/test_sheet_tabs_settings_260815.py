"""Quick task 260815-3hw (TABS-01/02/03): every Google Sheets tab NAME becomes an admin-editable
SETTINGS_SCHEMA entry ("sheets" group, screen «📄 Вкладки таблицы») instead of being scattered
between .env (GOOGLE_SHEET_TAB), hardcoded literals ("Гейма"/"История сдач"/"Незавершённые"/
"Отобранные"), and a couple of already-registered keys (short_sheet_tab/party_sheet_tab).

CLAUDE.md «бот для людей»: a manager renames any spreadsheet tab from a button, no developer,
no .env editing. Purpose is documented per-task below (mirrors 06-01-style TDD file structure —
one file grown across all three tasks of this plan, not three separate files).

Two critical invariants inherited from the 058def0 production incident and re-asserted here:
1. Positional resolution (`sh.sheet1`) is NEVER used anywhere in this file's assertions.
2. The main-tab resolution refusal (RuntimeError on stage 4) is never weakened.

pytest-asyncio is unavailable in this env (project convention, see tests/test_db_phase5.py) --
every async helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio
import sqlite3

import gspread

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import ADMIN_CAPS, required_capability
from settings_schema import SETTINGS_SCHEMA, _parse_setting
import services.sheets as sheets
import cities


ADMIN_ID = 931501


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_sheet_tabs_settings_260815.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _reset_sheets_module_state():
    """Mirrors tests/test_sheets_main_tab_pin_260813.py::_reset_module_state -- every test
    starts with no cached worksheet handle and no leftover one-shot warning flags."""
    sheets._reset_sheet_cache()
    sheets.set_alert_bot(None)
    sheets._alert_bot_warned = False
    sheets._startup_tab_warning_sent = False


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: registry group + «📄 Вкладки таблицы» screen
# ═══════════════════════════════════════════════════════════════════════════════════════════

# The literal old hardcodes, copied here as a frozen oracle -- this list IS the proof that the
# migration is a no-op (every registry default equals what used to be hardcoded in code).
_FROZEN_OLD_HARDCODE_DEFAULTS = {
    "main_sheet_tab": None,  # must stay None -- see _get_sheet() priority chain, Task 2
    "short_sheet_tab": "Краткая",
    "party_sheet_tab": "Party",
    "incomplete_sheet_tab": "Незавершённые",
    "game_matrix_tab": "Гейма",
    "game_history_tab": "История сдач",
    "preselect_tab": "Отобранные",
    "city_tab_suffix__short": " Акция",
    "city_tab_suffix__party": " Party",
    "city_tab_suffix__incomplete": " Незавершённые",
}


def test_sheets_group_keys_all_present_with_frozen_defaults():
    for key, expected_default in _FROZEN_OLD_HARDCODE_DEFAULTS.items():
        assert key in SETTINGS_SCHEMA, f"{key} missing from SETTINGS_SCHEMA"
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "sheets", f"{key} group {entry['group']!r} != 'sheets'"
        assert entry["type"] == "text", f"{key} type {entry['type']!r} != 'text'"
        assert entry["default"] == expected_default, (
            f"{key} default {entry['default']!r} != frozen oracle {expected_default!r} -- "
            "migration must be byte-for-byte no-op"
        )
        _parse_setting(key, entry.get("default"))  # must not raise


def test_settings_group_keys_sheets_matches_field_order():
    assert admin_mod._settings_group_keys("sheets") == admin_mod._SHEETS_FIELD_ORDER
    assert admin_mod._SHEETS_FIELD_ORDER == [
        "main_sheet_tab", "short_sheet_tab", "party_sheet_tab", "incomplete_sheet_tab",
        "game_matrix_tab", "game_history_tab", "preselect_tab",
        "city_tab_suffix__short", "city_tab_suffix__party", "city_tab_suffix__incomplete",
    ]


def test_short_and_party_sheet_tab_moved_out_of_old_groups():
    assert "short_sheet_tab" not in admin_mod._settings_group_keys("reg")
    assert "party_sheet_tab" not in admin_mod._settings_group_keys("party")
    # Both still reachable by the manager -- just on the new screen now, not vanished.
    assert "short_sheet_tab" in admin_mod._settings_group_keys("sheets")
    assert "party_sheet_tab" in admin_mod._settings_group_keys("sheets")
    # Leftover-safety net stays empty -- nothing silently fell into "Прочие".
    assert admin_mod._settings_group_keys("misc") == []


def test_settings_keyboard_has_sheets_group_nav_button():
    kb = asyncio.run(admin_mod.build_settings_keyboard())
    flat = _flat_callback_data(kb)
    assert "settings_group:sheets" in flat


def test_render_snapshot_sheets(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_settings_group_text("sheets"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("sheets"))
    flat = _flat_callback_data(kb)

    expected_keys = admin_mod._SHEETS_FIELD_ORDER
    edit_cbs = [cd for cd in flat if cd and cd.startswith("settings_edit:")]
    assert edit_cbs == [f"settings_edit:{k}" for k in expected_keys]

    # Fresh DB -> every non-None-default key shows "по умолчанию"; main_sheet_tab (default
    # None) shows the plain "не задано" flag (no display default -- see Task 2 priority chain).
    assert "📄 Основная (регистрации): <i>— не задано</i>" in text
    for key in expected_keys:
        if key == "main_sheet_tab":
            continue
        label = SETTINGS_SCHEMA[key]["label"]
        assert f"{label}: <i>по умолчанию</i>" in text, f"missing default flag for {label}"

    assert "admin_settings" in flat
    assert "settings_group_noop" in flat


def test_settings_edit_wildcard_covers_all_sheets_keys():
    """No new ADMIN_CAPS entries needed -- settings_edit:*/settings_group:* wildcards already
    cover every new key. Asserted here so a future ADMIN_CAPS refactor can't silently regress
    this without failing a test (grep is the plan's own acceptance check, this is the codified
    version)."""
    assert required_capability(callback_data="settings_group:sheets") == "settings"
    for key in admin_mod._SHEETS_FIELD_ORDER:
        assert required_capability(callback_data=f"settings_edit:{key}") == "settings"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: consumers read tab names from the registry, incl. the priority resolve chain
# ═══════════════════════════════════════════════════════════════════════════════════════════

# ── Fake gspread plumbing -- COPIED from tests/test_sheets_main_tab_pin_260813.py (plan's own
# instruction: copy, not import from a foreign test module) ────────────────────────────────

class _FakeWorksheet:
    def __init__(self, title):
        self.title = title


class _FakeSpreadsheet:
    """Ordered list of worksheets — .sheet1 is whichever is FIRST, mirroring gspread's real
    positional resolution. Kept so the tests can prove position is IGNORED: every case below
    puts a decoy tab first («STATISTICS», the real live-spreadsheet decoy) and asserts it is
    never the one written to."""

    def __init__(self, titles):
        self._sheets = {t: _FakeWorksheet(t) for t in titles}
        self._order = list(titles)

    @property
    def sheet1(self):
        raise AssertionError("sh.sheet1 must never be consulted -- positional resolve is dead (058def0)")

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


def _seed_setting(key, value):
    """Write straight into bot_settings via plain sqlite3 -- mirrors _get_sheet()'s own
    sync-only read path (services/sheets.py::_load_main_tab_setting), so this is the correct
    way to seed data for a test that calls the sync _get_sheet() directly."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ── Stage 1: bot_settings.main_sheet_tab wins over EVERYTHING, including position ──────────

def test_get_sheet_main_sheet_tab_setting_resolves_by_name(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    _seed_setting("main_sheet_tab", "Реги бот")
    _patch_gspread_client(monkeypatch, ["STATISTICS", "Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    ws = sheets._get_sheet()
    assert ws.title == "Реги бот"


def test_get_sheet_main_sheet_tab_setting_beats_env_when_both_set(tmp_path, monkeypatch):
    """Stage 1 (bot_settings) overrides stage 2 (.env) -- an admin naming the tab from the
    button is authoritative even if .env still carries an old/different value."""
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    _seed_setting("main_sheet_tab", "Из админки")
    _patch_gspread_client(monkeypatch, ["STATISTICS", "Из админки", "Из .env"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Из .env")

    ws = sheets._get_sheet()
    assert ws.title == "Из админки"


def test_get_sheet_main_sheet_tab_setting_auto_creates_when_missing(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    _seed_setting("main_sheet_tab", "Новая от менеджера")
    _patch_gspread_client(monkeypatch, ["STATISTICS"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    ws = sheets._get_sheet()
    assert ws.title == "Новая от менеджера"


# ── Stage 2: GOOGLE_SHEET_TAB still works when bot_settings is unset (back-compat) ─────────

def test_get_sheet_env_tab_still_resolves_by_name_when_setting_unset(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    _patch_gspread_client(monkeypatch, ["STATISTICS", "Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги бот")

    ws = sheets._get_sheet()
    assert ws.title == "Реги бот"


# ── Stage 4: nothing configured anywhere -- refuse, never touch sh.sheet1 ──────────────────

def test_get_sheet_refuses_when_nothing_configured_at_all(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    _patch_gspread_client(monkeypatch, ["STATISTICS", "Реги бот"])
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    try:
        sheets._get_sheet()
        assert False, "expected _get_sheet() to refuse when nothing names the main tab"
    except RuntimeError as e:
        assert "GOOGLE_SHEET_TAB" in str(e)


# ── _tab_explicitly_configured(): unblocked by EITHER stage 1 or stage 2 ───────────────────

def test_tab_explicitly_configured_true_from_bot_settings_alone(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("main_sheet_tab", "Реги бот"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    assert asyncio.run(sheets._tab_explicitly_configured()) is True


def test_tab_explicitly_configured_false_when_neither_set(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    assert asyncio.run(sheets._tab_explicitly_configured()) is False


def test_rebuild_main_sheet_allowed_when_only_bot_settings_names_the_tab(tmp_path, monkeypatch):
    """A manager who names the main tab purely from the admin screen (no GOOGLE_SHEET_TAB in
    .env at all) must be able to use «♻️ Пересобрать таблицу» -- this is the exact tuple this
    plan exists to close (see plan objective)."""
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("main_sheet_tab", "Реги бот"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    def fake_rebuild_sync(headers, rows):
        return len(rows)

    monkeypatch.setattr(sheets, "_rebuild_main_sheet_sync", fake_rebuild_sync)

    async def go():
        return await sheets.rebuild_main_sheet(["A"], [[1], [2]])

    assert asyncio.run(go()) == 2


# ── Named tab consumers: game matrix/history, city incomplete, city suffix, preselect ──────

def test_sync_game_sheets_uses_registry_tab_names_by_default(tmp_path, monkeypatch):
    from database import db as db_mod

    _use_tmp_db(tmp_path)
    asyncio.run(db_mod.init_db())
    config.ADMIN_IDS = [ADMIN_ID]

    calls = []

    async def _fake_sync(title, headers, rows):
        calls.append(title)
        return len(rows)

    monkeypatch.setattr(admin_mod, "sync_named_worksheet", _fake_sync)

    class _FakeUser:
        def __init__(self, uid):
            self.id = uid

    class _FakeMsg:
        def __init__(self):
            self.answers_sent = []

        async def answer(self, text, parse_mode=None, reply_markup=None):
            self.answers_sent.append(text)

    class _FakeCallback:
        def __init__(self):
            self.from_user = _FakeUser(ADMIN_ID)
            self.message = _FakeMsg()

        async def answer(self, text=None, show_alert=False):
            pass

    callback = _FakeCallback()

    async def go():
        await admin_mod.sync_game_sheets(callback)

    asyncio.run(go())
    assert calls == ["Гейма", "История сдач"]


def test_sync_game_sheets_confirm_shows_renamed_tabs(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("game_matrix_tab", "GAMIFICATION бот"))
    asyncio.run(db.set_setting("game_history_tab", "GAMIFICATION чат"))

    class _FakeUser:
        def __init__(self, uid):
            self.id = uid

    class _FakeMsg:
        def __init__(self):
            self.text = None
            self.markup = None

        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.text = text
            self.markup = reply_markup

    class _FakeCallback:
        def __init__(self):
            self.from_user = _FakeUser(ADMIN_ID)
            self.message = _FakeMsg()

        async def answer(self, text=None, show_alert=False):
            pass

    callback = _FakeCallback()

    async def go():
        await admin_mod.sync_game_sheets_confirm(callback)

    asyncio.run(go())
    assert "GAMIFICATION бот" in callback.message.text
    assert "GAMIFICATION чат" in callback.message.text
    assert "Гейма" not in callback.message.text
    assert "История сдач" not in callback.message.text


def test_city_incomplete_tab_default_reads_registry(tmp_path):
    from handlers import registration as reg

    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg.city_incomplete_tab(None)

    assert asyncio.run(go()) == "Незавершённые"


def test_city_incomplete_tab_custom_default_from_registry(tmp_path):
    from handlers import registration as reg

    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("incomplete_sheet_tab", "Не доделали")
        return await reg.city_incomplete_tab(None)

    assert asyncio.run(go()) == "Не доделали"


def test_tab_suffix_defaults_match_old_hardcodes():
    async def go():
        await db.init_db()
        return (
            await cities.tab_suffix("short"),
            await cities.tab_suffix("party"),
            await cities.tab_suffix("incomplete"),
            await cities.tab_suffix("main"),
        )

    results = asyncio.run(go())
    assert results == (" Акция", " Party", " Незавершённые", "")


def test_tab_suffix_normalizes_missing_leading_space(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("city_tab_suffix__short", "Акция")  # no leading space
        return await cities.tab_suffix("short")

    assert asyncio.run(go()) == " Акция"


def test_city_row_tab_uses_tab_suffix_helper(tmp_path):
    from handlers import registration as reg

    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_tab_suffix__short", "Промо")
        return await reg.city_row_tab("spb", "short")

    assert asyncio.run(go()) == "СПб Промо"


def test_refresh_allowlist_reads_preselect_tab_via_registry(tmp_path, monkeypatch):
    from services import allowlist

    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()

        captured = {}

        def fake_rows(tab_name):
            captured["tab"] = tab_name
            return ["header", "someone"]

        monkeypatch.setattr("services.sheets._get_allowlist_rows_sync", fake_rows)
        await allowlist.refresh_allowlist()
        return captured["tab"]

    assert asyncio.run(go()) == "Отобранные"


def test_grep_no_sheet1_attribute_access():
    """Codified version of the plan's own acceptance grep: `sh.sheet1` (the gspread positional
    accessor, actual CODE not prose) must never appear anywhere in services/sheets.py. Comments
    and docstrings explaining the 058def0 incident by name ("sheet1") are fine and excluded --
    tracked with a simple triple-quote/# toggle rather than a full tokenizer, good enough for
    this one file."""
    import pathlib

    src = pathlib.Path(sheets.__file__).read_text(encoding="utf-8")
    in_docstring = False
    for line in src.splitlines():
        stripped = line.strip()
        quote_count = line.count('"""')
        if quote_count % 2 == 1:
            in_docstring = not in_docstring
            continue  # the line that opens/closes a docstring is prose either way
        if in_docstring or stripped.startswith("#"):
            continue
        assert "sh.sheet1" not in line, f"positional resolve reference found: {line!r}"
