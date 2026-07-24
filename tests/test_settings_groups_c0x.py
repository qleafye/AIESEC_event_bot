"""Quick 260724-c0x tests: settings landing screen no longer dumps ~40 fields inline;
fields are grouped into per-group sub-screens (settings_group:{token}) with status flags
(«задано»/«не задано»/«по умолчанию») instead of raw values. Existing edit/photo/file/toggle
callbacks stay byte-identical — only render/navigation changed.

pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async
helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod


ADMIN_ID = 900002


def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_groups_c0x.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


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


# ── Task 1: coverage — every SETTINGS_FIELDS key lands in exactly one group (or leftover) ──

def test_settings_groups_cover_every_field_key():
    grouped_keys = [k for _, __, keys in admin_mod.SETTINGS_GROUPS for k in keys]
    all_keys = [k for k, _, _ in admin_mod.SETTINGS_FIELDS]
    leftover = admin_mod._settings_group_keys("misc")

    # No duplicates within declared groups.
    assert len(grouped_keys) == len(set(grouped_keys))

    for key in all_keys:
        assert key in grouped_keys or key in leftover, f"{key} missing from all groups"

    # Nothing invented: every grouped/leftover key must be a real SETTINGS_FIELDS key.
    for key in grouped_keys + leftover:
        assert key in all_keys


# ── Task 1: landing no longer dumps values inline ───────────────────────────────────

def test_landing_text_has_no_inline_value_dump(tmp_path):
    _admin_ready(tmp_path)
    long_value = "x" * 200
    asyncio.run(db.set_setting("start_text", long_value))

    text = asyncio.run(admin_mod.render_settings_text())

    assert ("x" * 61) not in text
    assert "…" not in text


def test_landing_keyboard_emits_group_nav_not_per_field(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_settings_keyboard())
    flat = _flat_callback_data(kb)

    assert any(cd.startswith("settings_group:") for cd in flat)
    assert not any(cd and cd.startswith("settings_edit:") for cd in flat)
    assert not any(cd and cd.startswith("settings_photo:") for cd in flat)
    assert not any(cd and cd.startswith("settings_file:") for cd in flat)

    # Prior toggle/back callbacks must still be present, untouched.
    assert "toggle_payment_enabled" in flat
    assert "settings_toggle_reg" in flat
    assert "settings_back" in flat


# ── Task 2: per-group sub-screen — flags, collapse, callback integrity ─────────────

def test_group_pay_shows_configured_and_unconfigured_flags(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("payment_options", "Полный билет|5000"))

    text = asyncio.run(admin_mod.render_settings_group_text("pay"))
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)

    assert "✏️ задано" in text
    assert "не задано" in text
    assert "settings_edit:payment_options" in flat
    assert "admin_settings" in flat  # back button reuses existing landing handler


def test_group_event_contains_photo_and_file_callbacks(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("event"))
    flat = _flat_callback_data(kb)

    assert any(cd.startswith("settings_photo:") for cd in flat)
    assert any(cd.startswith("settings_file:") for cd in flat)
    assert any(cd.startswith("settings_edit:") for cd in flat)


def test_group_keyboard_collapses_unconfigured_under_noop_header(tmp_path):
    _admin_ready(tmp_path)
    # pay group has 7 keys, none set -> all unconfigured -> noop header must appear.
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("pay"))
    flat = _flat_callback_data(kb)
    assert "settings_group_noop" in flat


def test_show_settings_group_handler_renders_subscreen(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group:pay")
    asyncio.run(admin_mod.show_settings_group(cb))

    assert cb.message.edit_calls == 1
    assert "Оплата" in cb.message.text
    flat = _flat_callback_data(cb.message.markup)
    assert "settings_edit:payment_options" in flat


def test_show_settings_group_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group:pay", user_id=1)
    asyncio.run(admin_mod.show_settings_group(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True


def test_settings_group_noop_just_answers(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_group_noop")
    asyncio.run(admin_mod.settings_group_noop(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers
