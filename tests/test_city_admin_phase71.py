"""Phase 07.1 Plan 04 (CITY-04) tests: admin «🏙 Города мероприятия» screen, per-city
«Незавершённые» batching (manual export + 2h auto-sync parity), and doc-string-safety
checks tying ADMIN_GUIDE.md to the implemented tab names/tokens.

pytest-asyncio is unavailable in this env — every async helper is driven via
asyncio.run() and config.DB_PATH points at a tmp_path file (same idiom as
tests/test_admin_phase5.py).
"""
import asyncio

import aiosqlite

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import registration as reg_mod
from cities import CITIES


ADMIN_ID = 900101
NON_ADMIN_ID = 900102


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_admin71.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


async def _settings_row_count() -> int:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute("SELECT COUNT(*) FROM bot_settings") as cur:
            row = await cur.fetchone()
            return row[0]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


CITY_CODES = [c["code"] for c in CITIES]  # msk/spb/tyumen from .env defaults


# ── Task 1: «🏙 Города мероприятия» admin screen ──────────────────────────────

def test_build_admin_keyboard_admin_cities_is_last_row_indices_unchanged():
    kb = admin_mod.build_admin_keyboard()
    rows = kb.inline_keyboard
    assert rows[-1][0].callback_data == "admin_cities"
    expected_first_13 = [
        "admin_stats", "admin_monthly_stats", "admin_source_stats", "admin_export_csv",
        "admin_export_incomplete", "admin_applications", "admin_receipts", "admin_broadcast",
        "admin_sync_sheet", "admin_rebuild_sheet", "admin_dedupe_sheet", "admin_settings",
        "admin_settings_guide",
    ]
    actual_first_13 = [rows[i][0].callback_data for i in range(13)]
    assert actual_first_13 == expected_first_13


def test_build_cities_keyboard_contains_toggle_and_per_city_buttons(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_cities_keyboard())
    flat = _flat_callback_data(kb)
    assert "toggle_event_city_enabled" in flat
    for code in CITY_CODES:
        assert f"city_toggle:{code}" in flat
        assert f"settings_edit:city_label__{code}" in flat
    assert len(CITY_CODES) == 3


def test_admin_cities_screen_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("admin_cities", user_id=NON_ADMIN_ID)
    asyncio.run(admin_mod.show_admin_cities(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert cb.message.edit_calls == 0
    assert asyncio.run(_settings_row_count()) == 0


def test_toggle_event_city_enabled_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("toggle_event_city_enabled", user_id=NON_ADMIN_ID)
    asyncio.run(admin_mod.toggle_event_city_enabled(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert asyncio.run(_settings_row_count()) == 0


def test_city_toggle_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("city_toggle:spb", user_id=NON_ADMIN_ID)
    asyncio.run(admin_mod.city_toggle(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert asyncio.run(_settings_row_count()) == 0


def test_toggle_event_city_enabled_flips_off_to_on_to_off(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(admin_mod.toggle_event_city_enabled(FakeCallback("toggle_event_city_enabled")))
    assert asyncio.run(db.get_setting("event_city_enabled")) == "on"
    asyncio.run(admin_mod.toggle_event_city_enabled(FakeCallback("toggle_event_city_enabled")))
    assert asyncio.run(db.get_setting("event_city_enabled")) == "off"


def test_city_toggle_spb_flips_default_on_to_off_to_on(tmp_path):
    _admin_ready(tmp_path)
    # default (no bot_settings row) resolves to "on" — cities.is_city_enabled
    asyncio.run(admin_mod.city_toggle(FakeCallback("city_toggle:spb")))
    assert asyncio.run(db.get_setting("city_enabled__spb")) == "off"
    asyncio.run(admin_mod.city_toggle(FakeCallback("city_toggle:spb")))
    assert asyncio.run(db.get_setting("city_enabled__spb")) == "on"


def test_city_toggle_unknown_code_rejected_no_write(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("city_toggle:atlantis")
    asyncio.run(admin_mod.city_toggle(cb))
    assert cb.answers[-1][1] is True  # show_alert
    assert asyncio.run(_settings_row_count()) == 0


def test_render_cities_text_has_deep_link_and_label_escaped(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_cities_text())
    assert "?start=city_spb" in text
    spb_label = asyncio.run(db.get_setting("city_label__spb"))
    assert spb_label is None  # unset -> falls back to .env label, not asserted verbatim here

    asyncio.run(db.set_setting("city_label__spb", "<b>x</b>"))
    text2 = asyncio.run(admin_mod.render_cities_text())
    assert "<b>x</b>" not in text2
    assert "&lt;b&gt;x&lt;/b&gt;" in text2
