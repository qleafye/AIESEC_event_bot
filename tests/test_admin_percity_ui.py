"""Phase 09.2 Plan 05 (C, CITY-05) tests: admin-UI per-city override sub-flow —
«🏙 Для города…» screen, city-scoped edit/clear, and the group-screen override summary.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_settings_groups_c0x.py / tests/test_city_admin_phase72.py.

Sections mirror the plan's tasks:
    Task 1 — entry points, visibility, human-only labels, ADMIN_CAPS coverage.
    Task 2 — write/clear round-trip, enum toggle, unknown-code refusal, bound-manager gate.
    Task 3 — group-screen «🏙 N» summary + full synthetic bound-manager scenario.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability, role_caps_key, role_enabled_key
import cities


ADMIN_ID = 920501
MANAGER_ID = 920502
OTHER_MANAGER_ID = 920503


def _admin_ready(tmp_path, db_name="test_admin_percity_ui.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


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


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data = {}
        self.state = None


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_button_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: entry points, visibility, human-only labels, ADMIN_CAPS coverage
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_all_four_prefixes_covered_by_admin_caps():
    for cb in (
        "settings_city:start_text",
        "settings_city_pick:start_text:msk",
        "settings_city_clear:start_text:msk",
        "settings_city_clear_go:start_text:msk",
    ):
        assert required_capability(callback_data=cb) == "settings", cb


def test_module_off_editor_has_only_cancel_button(tmp_path):
    _admin_ready(tmp_path)
    # cities module left at its "off" default.
    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    data = _flat_callback_data(cb.message.markup)
    assert data == ["settings_cancel"]
    assert "🏙" not in cb.message.text


def test_module_on_per_city_key_editor_has_city_row(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    data = _flat_callback_data(cb.message.markup)
    assert "settings_city:start_text" in data


def test_module_on_non_per_city_key_editor_has_no_city_row(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    assert not cities.is_per_city("event_name")
    cb = FakeCallback("settings_edit:event_name")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    data = _flat_callback_data(cb.message.markup)
    assert not any(d and d.startswith("settings_city:") for d in data)


def test_settings_city_list_fail_closed_when_module_off(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_city:start_text")
    asyncio.run(admin_mod.settings_city_list(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True


def test_settings_city_list_fail_closed_for_non_per_city_key(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    cb = FakeCallback("settings_city:event_name")
    asyncio.run(admin_mod.settings_city_list(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True


def test_settings_city_list_shows_labels_marks_and_no_codes(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    cb = FakeCallback("settings_city:start_text")
    asyncio.run(admin_mod.settings_city_list(cb))

    text = cb.message.text
    kb = cb.message.markup
    button_texts = _flat_button_texts(kb)
    codes = cities.city_codes()
    assert not any(code in t for t in button_texts for code in codes)
    assert not any(code in text for code in codes)
    spb_label = asyncio.run(cities.city_label("spb"))
    assert any(spb_label in t and "✅" in t for t in button_texts)
    msk_label = asyncio.run(cities.city_label("msk"))
    assert any(msk_label in t and "—" in t for t in button_texts)
    assert "Переопределено для" in text
    assert spb_label in text


def test_settings_city_list_header_shows_global_or_default(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    cb = FakeCallback("settings_city:start_text")
    asyncio.run(admin_mod.settings_city_list(cb))
    assert "по умолчанию" in cb.message.text


def test_landing_shows_percity_button_only_when_module_on(tmp_path):
    _admin_ready(tmp_path)
    kb_off = asyncio.run(admin_mod.build_settings_keyboard())
    assert "settings_city:registration_mode" not in _flat_callback_data(kb_off)

    _enable_cities()
    kb_on = asyncio.run(admin_mod.build_settings_keyboard())
    assert "settings_city:registration_mode" in _flat_callback_data(kb_on)


def test_superadmin_sees_all_cities_in_picker(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    codes = asyncio.run(admin_mod._per_city_visible_codes(ADMIN_ID))
    assert set(codes) == set(cities.city_codes())


