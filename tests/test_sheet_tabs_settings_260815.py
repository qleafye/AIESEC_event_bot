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
