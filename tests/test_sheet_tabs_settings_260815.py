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
from handlers import admin_gamification
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

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)

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
        await admin_gamification.sync_game_sheets(callback)

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
        await admin_gamification.sync_game_sheets_confirm(callback)

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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 3: confirm-gate on an existing tab + main-tab cache reset
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeSettingsMessage:
    """Stand-in for aiogram Message -- only the attributes/methods settings_edit_value and
    the sheets_tab_confirm/cancel handlers actually touch."""

    def __init__(self, uid=ADMIN_ID, text=""):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.html_text = text
        self.answers = []  # list of (text, kwargs)
        # For CallbackQuery.message.edit_text usage (confirm/cancel handlers):
        self.edited_text = None
        self.edited_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited_text = text
        self.edited_markup = reply_markup


class _FakeSettingsCallback:
    def __init__(self, uid=ADMIN_ID):
        self.from_user = _FakeUser(uid)
        self.message = _FakeSettingsMessage(uid)
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append((text, show_alert))


class _FakeFSMState:
    """Plain-dict backed FSMContext stand-in (copied convention from
    tests/test_short_track_phase7.py::_FakeState), extended with set_state/get_state since
    settings_edit_value's confirm-gate branch calls both."""

    def __init__(self, data=None):
        self._data = dict(data or {})
        self._state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


def _edit_setting_state(key):
    return _FakeFSMState({"setting_key": key})


def test_existing_write_tab_shows_confirm_and_does_not_save(tmp_path, monkeypatch):
    _admin_ready(tmp_path)

    async def fake_probe(title):
        assert title == "GAMIFICATION бот"
        return (True, 30)

    monkeypatch.setattr(admin_mod, "tab_row_count", fake_probe)

    message = _FakeSettingsMessage(text="GAMIFICATION бот")
    state = _edit_setting_state("game_matrix_tab")

    async def go():
        await admin_mod.settings_edit_value(message, state)
        return await db.get_setting("game_matrix_tab")

    saved = asyncio.run(go())
    assert saved is None  # NOT saved yet -- awaiting confirmation
    assert state._state == admin_mod.EditSetting.waiting_for_tab_confirm
    pending = asyncio.run(state.get_data())
    assert pending["pending_tab_key"] == "game_matrix_tab"
    assert pending["pending_tab_value"] == "GAMIFICATION бот"

    confirm_text = message.answers[-1][0]
    assert "GAMIFICATION бот" in confirm_text
    assert "30" in confirm_text
    assert "перезапис" in confirm_text.lower()
    assert "пропад" in confirm_text.lower()


def test_confirm_saves_pending_value_and_returns_to_sheets_screen(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    state = _FakeFSMState({"pending_tab_key": "game_matrix_tab", "pending_tab_value": "GAMIFICATION бот"})
    callback = _FakeSettingsCallback()

    async def go():
        await admin_mod.sheets_tab_confirm_go(callback, state)
        return await db.get_setting("game_matrix_tab")

    saved = asyncio.run(go())
    assert saved == "GAMIFICATION бот"
    assert state._data == {}  # cleared
    assert "Вкладки таблицы" in callback.message.edited_text


def test_cancel_does_not_save_pending_value(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    state = _FakeFSMState({"pending_tab_key": "game_matrix_tab", "pending_tab_value": "GAMIFICATION бот"})
    callback = _FakeSettingsCallback()

    async def go():
        await admin_mod.sheets_tab_cancel_go(callback, state)
        return await db.get_setting("game_matrix_tab")

    saved = asyncio.run(go())
    assert saved is None
    assert state._data == {}
    assert "Вкладки таблицы" in callback.message.edited_text


def test_missing_tab_saves_silently_no_confirm_screen(tmp_path, monkeypatch):
    _admin_ready(tmp_path)

    async def fake_probe(title):
        return (False, 0)

    monkeypatch.setattr(admin_mod, "tab_row_count", fake_probe)

    message = _FakeSettingsMessage(text="Новая вкладка")
    state = _edit_setting_state("game_matrix_tab")

    async def go():
        await admin_mod.settings_edit_value(message, state)
        return await db.get_setting("game_matrix_tab")

    saved = asyncio.run(go())
    assert saved == "Новая вкладка"
    assert len(message.answers) == 1  # the normal post-save settings screen, not a confirm screen
    assert "перезапис" not in message.answers[0][0].lower()


def test_probe_failure_saves_with_warning(tmp_path, monkeypatch):
    _admin_ready(tmp_path)

    async def fake_probe(title):
        return None  # Sheets unreachable/unconfigured

    monkeypatch.setattr(admin_mod, "tab_row_count", fake_probe)

    message = _FakeSettingsMessage(text="Какая-то вкладка")
    state = _edit_setting_state("incomplete_sheet_tab")

    async def go():
        await admin_mod.settings_edit_value(message, state)
        return await db.get_setting("incomplete_sheet_tab")

    saved = asyncio.run(go())
    assert saved == "Какая-то вкладка"
    text = message.answers[-1][0]
    assert "проверить вкладку" in text.lower()


def test_preselect_and_suffix_keys_never_trigger_the_gate(tmp_path, monkeypatch):
    """preselect_tab (read-only) and city_tab_suffix__* (not full tab names) must not even
    call tab_row_count -- calling it would be a wasted/misleading API probe."""
    _admin_ready(tmp_path)

    def fail_if_called(title):
        raise AssertionError(f"tab_row_count must not be called for this key (title={title!r})")

    monkeypatch.setattr(admin_mod, "tab_row_count", fail_if_called)

    for key, value in [("preselect_tab", "Мой список"), ("city_tab_suffix__short", "Промо")]:
        message = _FakeSettingsMessage(text=value)
        state = _edit_setting_state(key)

        async def go(message=message, state=state, key=key):
            await admin_mod.settings_edit_value(message, state)
            return await db.get_setting(key)

        assert asyncio.run(go()) == value


def test_saving_main_sheet_tab_resets_sheet_cache_on_all_three_paths(tmp_path, monkeypatch):
    reset_calls = []
    monkeypatch.setattr(admin_mod, "_reset_sheet_cache", lambda: reset_calls.append(1))

    async def fake_probe_missing(title):
        return (False, 0)

    monkeypatch.setattr(admin_mod, "tab_row_count", fake_probe_missing)

    # Path 1: plain save (tab doesn't exist yet).
    _admin_ready(tmp_path)
    message = _FakeSettingsMessage(text="Реги бот")
    state = _edit_setting_state("main_sheet_tab")
    asyncio.run(admin_mod.settings_edit_value(message, state))
    assert len(reset_calls) == 1

    # Path 2: "-" clear.
    message = _FakeSettingsMessage(text="-")
    state = _edit_setting_state("main_sheet_tab")
    asyncio.run(admin_mod.settings_edit_value(message, state))
    assert len(reset_calls) == 2

    # Path 3: save-after-confirm.
    state = _FakeFSMState({"pending_tab_key": "main_sheet_tab", "pending_tab_value": "Ещё вкладка"})
    callback = _FakeSettingsCallback()
    asyncio.run(admin_mod.sheets_tab_confirm_go(callback, state))
    assert len(reset_calls) == 3


def test_sheets_tab_confirm_and_cancel_are_capability_mapped():
    assert "sheets_tab_confirm" in ADMIN_CAPS
    assert "sheets_tab_cancel" in ADMIN_CAPS
    assert ADMIN_CAPS["sheets_tab_confirm"] == "settings"
    assert ADMIN_CAPS["sheets_tab_cancel"] == "settings"
    assert required_capability(callback_data="sheets_tab_confirm") == "settings"
    assert required_capability(callback_data="sheets_tab_cancel") == "settings"
