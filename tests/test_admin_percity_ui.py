"""Phase 09.3 Plan 06 (CITY-09) tests: settings editor relative to the admin-panel header —
«✏️ Изменить для {город}» / «↩️ Как везде», the removed 09.2-05 picker family, and the
group-screen override summary (Task 3 section, unaffected by this plan, left as-is).

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_settings_groups_c0x.py / tests/test_city_admin_phase72.py.

Sections:
    Header-scoped editor — entry points, ADMIN_CAPS coverage, own-city view, edit/reset
    round-trip, bound-manager gate, freshness check (this plan's own scope).
    Group screen «🏙 N» summary — Phase 09.3-05, unmodified by this plan.
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


class FakeMsgIn:
    def __init__(self, text, user_id=ADMIN_ID):
        self.text = text
        self.html_text = text
        self.from_user = FakeUser(user_id)

    async def answer(self, *a, **kw):
        pass


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_button_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _add_bound_manager(manager_id=MANAGER_ID, city="spb"):
    asyncio.run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(manager_id, city))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg;moderate_receipts;settings"))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Header-scoped editor: entry points, ADMIN_CAPS coverage, module-off/«все города» parity
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_new_prefixes_covered_by_admin_caps_old_prefixes_gone():
    for cb in (
        "settings_edit_city:start_text",
        "settings_reset_city:start_text",
        "settings_reset_city_go:start_text:msk",
    ):
        assert required_capability(callback_data=cb) == "settings", cb
    # 09.2-05's four-entry picker family must be gone from ADMIN_CAPS entirely — a forgotten
    # entry would mean deny-by-default silently passes if the deleted handler ever came back.
    for cb in (
        "settings_city:start_text",
        "settings_city_pick:start_text:msk",
        "settings_city_clear:start_text:msk",
        "settings_city_clear_go:start_text:msk",
    ):
        assert required_capability(callback_data=cb) is None, cb


def test_module_off_editor_has_only_cancel_button(tmp_path):
    _admin_ready(tmp_path)
    # cities module left at its "off" default -> byte-identical to before the phase.
    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    data = _flat_callback_data(cb.message.markup)
    assert data == ["settings_cancel"]
    assert "🏙" not in cb.message.text
    assert state.state == admin_mod.EditSetting.waiting_for_value
    assert state.data.get("setting_key") == "start_text"


def test_all_cities_header_editor_has_no_edit_city_button_but_keeps_override_summary(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    data = _flat_callback_data(cb.message.markup)
    assert not any(d and d.startswith("settings_edit_city:") for d in data)
    assert data == ["settings_cancel"]
    assert "Переопределено для" in cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text
    # FSM starts immediately in this branch — editing the global value, same as today.
    assert state.state == admin_mod.EditSetting.waiting_for_value
    assert state.data.get("setting_key") == "start_text"


def test_module_on_non_per_city_key_editor_marked_global_only(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    assert not cities.is_per_city("event_name")

    cb = FakeCallback("settings_edit:event_name")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    data = _flat_callback_data(cb.message.markup)
    assert not any(d and d.startswith("settings_edit_city:") for d in data)
    assert "Общая настройка (одна на все города)" in cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert cb.message.text.splitlines()[0] == f"🏙 {spb_label}"
    # FSM starts immediately -- the key is edited globally, there is no city-scoped step.
    assert state.state == admin_mod.EditSetting.waiting_for_value
    assert state.data.get("setting_key") == "event_name"


def test_landing_never_shows_percity_picker_button_for_registration_mode(tmp_path):
    """Phase 09.3-04 (CITY-09): the picker shortcut («🏙 Форма по городам» ->
    settings_city:registration_mode) is gone permanently -- registration_mode is now edited
    per-city through the admin-panel header toggle (tests/test_regmode_header_093.py), never
    through this editor, module on or off, with or without a header selected."""
    _admin_ready(tmp_path)
    kb_off = asyncio.run(admin_mod.build_settings_keyboard())
    assert "settings_city:registration_mode" not in _flat_callback_data(kb_off)
    assert "settings_edit_city:registration_mode" not in _flat_callback_data(kb_off)

    _enable_cities()
    kb_on = asyncio.run(admin_mod.build_settings_keyboard())
    assert "settings_city:registration_mode" not in _flat_callback_data(kb_on)
    assert "settings_edit_city:registration_mode" not in _flat_callback_data(kb_on)


def test_superadmin_visible_codes_cover_all_cities(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    codes = asyncio.run(admin_mod._per_city_visible_codes(ADMIN_ID))
    assert set(codes) == set(cities.city_codes())


# ══════════════════════════════════════════════════════════════════════════════════════════
# Header-scoped editor: own-city view (CONTEXT B branch 1)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_own_city_editor_shows_own_value_and_reset_button(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    spb_label = asyncio.run(cities.city_label("spb"))
    assert cb.message.text.splitlines()[0] == f"🏙 {spb_label}"
    assert "Своё значение:" in cb.message.text
    assert "Питерский текст" in cb.message.text
    codes = cities.city_codes()
    assert not any(code in cb.message.text for code in codes)  # no raw codes, only labels
    data = _flat_callback_data(cb.message.markup)
    assert "settings_edit_city:start_text" in data
    assert "settings_reset_city:start_text" in data


def test_own_city_editor_shows_as_everywhere_when_no_own_value(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting("start_text", "Общий текст"))

    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    assert "Как везде. Общий текст:" in cb.message.text
    assert "Общий текст" in cb.message.text
    data = _flat_callback_data(cb.message.markup)
    assert "settings_edit_city:start_text" in data
    assert "settings_reset_city:start_text" not in data  # nothing to reset yet


def test_own_city_editor_does_not_start_fsm(tmp_path):
    """A manager just LOOKING at the own-city editor (no tap yet) must never have a stray
    text message land in the global value — only «✏️ Изменить для …» starts the FSM."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    assert state.state is None
    assert state.data == {}


def test_own_city_editor_module_on_but_no_override_shows_default_line(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_edit:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))

    assert "по умолчанию" in cb.message.text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Header-scoped editor: «✏️ Изменить для …» — the only FSM entry point for a composite key
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_settings_edit_city_opens_composite_fsm(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_edit_city:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(cb, state))

    assert state.data.get("setting_key") == cities.per_city_key("start_text", "spb")
    assert state.data.get("per_city_base") == "start_text"
    assert state.state == admin_mod.EditSetting.waiting_for_value
    data = _flat_callback_data(cb.message.markup)
    assert data == ["settings_edit:start_text"]  # no own value yet -> no reset row


def test_settings_edit_city_fails_closed_when_module_off(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_edit_city:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(cb, state))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True
    assert state.data == {}


def test_settings_edit_city_fails_closed_for_non_per_city_key(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("settings_edit_city:event_name")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(cb, state))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True
    assert state.data == {}


def test_settings_edit_city_refuses_when_header_is_all_cities(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("settings_edit_city:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(cb, state))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True
    assert state.data == {}


def test_settings_edit_city_refused_when_not_visible_defense_in_depth(tmp_path, monkeypatch):
    """T-093-19/21: the RIGHT is re-checked in the handler itself, not just by hiding the
    button -- forced via monkeypatch since a real bound manager's header can never diverge
    from their own visible set (admin_selected_city already locks it)."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    async def _empty_visible(admin_id):
        return []
    monkeypatch.setattr(admin_mod, "_per_city_visible_codes", _empty_visible)

    cb = FakeCallback("settings_edit_city:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(cb, state))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True
    assert state.data == {}


def test_bound_manager_can_edit_only_own_city_via_header(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    cb = FakeCallback("settings_edit_city:start_text", user_id=MANAGER_ID)
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(cb, state))

    assert state.data.get("setting_key") == cities.per_city_key("start_text", "spb")
    assert state.data.get("per_city_base") == "start_text"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Header-scoped editor: write/clear round-trip, unchanged global, return-to-editor
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_text_roundtrip_write_read_clear_via_header_does_not_touch_global(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting("start_text", "Глобальный текст"))

    edit_cb = FakeCallback("settings_edit_city:start_text")
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(edit_cb, state))

    msg = FakeMsgIn("Питерский текст")
    asyncio.run(admin_mod.settings_edit_value(msg, state))

    assert asyncio.run(db.get_setting("start_text")) == "Глобальный текст"
    resolved = asyncio.run(cities.get_setting_for_city("start_text", "spb"))
    assert resolved == "Питерский текст"
    # After save the screen returns to the SAME header-aware editor, not the landing.
    assert state.data == {} and state.state is None

    # Clear via "-" — composite key deleted, global untouched, still returns to the editor.
    edit_cb2 = FakeCallback("settings_edit_city:start_text")
    state2 = FakeState()
    asyncio.run(admin_mod.settings_edit_city(edit_cb2, state2))
    msg_clear = FakeMsgIn("-")
    asyncio.run(admin_mod.settings_edit_value(msg_clear, state2))

    assert asyncio.run(db.get_setting("start_text")) == "Глобальный текст"
    resolved_after = asyncio.run(cities.get_setting_for_city("start_text", "spb"))
    assert resolved_after == "Глобальный текст"


def test_html_settings_branch_checked_against_base_key():
    import inspect
    src = inspect.getsource(admin_mod.settings_edit_value)
    assert "_base_setting_key(key) in HTML_SETTINGS" in src


def test_no_per_city_key_in_sheet_tab_write_mode_or_options_suffix():
    per_city_keys = [
        k for k, v in __import__("settings_schema").SETTINGS_SCHEMA.items() if v.get("per_city")
    ]
    for k in per_city_keys:
        assert k not in admin_mod._SHEET_TAB_WRITE_MODE, k
        assert not k.endswith("_options"), k


# ══════════════════════════════════════════════════════════════════════════════════════════
# Header-scoped editor: «↩️ Как везде» confirm/go, freshness check, bound-manager gate
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reset_confirm_refuses_when_no_own_value(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_reset_city:start_text")
    asyncio.run(admin_mod.settings_reset_city(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True


def test_reset_confirm_then_go_returns_to_editor_view(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    confirm_cb = FakeCallback("settings_reset_city:start_text")
    asyncio.run(admin_mod.settings_reset_city(confirm_cb))
    # Confirm screen previews the GLOBAL text the city is about to fall back to (unset here
    # -> "по умолчанию"), not the override being removed — and names the consequence.
    assert "по умолчанию" in confirm_cb.message.text
    assert "будет удалён" in confirm_cb.message.text

    go_cb = FakeCallback("settings_reset_city_go:start_text:spb")
    asyncio.run(admin_mod.settings_reset_city_go(go_cb))

    resolved = asyncio.run(cities.get_setting_for_city("start_text", "spb"))
    assert resolved is None or resolved == ""
    assert "Как везде. Общий текст:" in go_cb.message.text  # back on the editor view

    # Idempotent second go.
    go_cb2 = FakeCallback("settings_reset_city_go:start_text:spb")
    asyncio.run(admin_mod.settings_reset_city_go(go_cb2))
    assert go_cb2.answers and go_cb2.answers[0][1] is True


def test_reset_go_unknown_city_code_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    cb = FakeCallback("settings_reset_city_go:start_text:atlantis")
    asyncio.run(admin_mod.settings_reset_city_go(cb))

    assert cb.answers and cb.answers[0][1] is True
    assert cb.message.edit_calls == 0


def test_reset_go_freshness_check_refuses_after_header_changed(tmp_path):
    """T-093-22: the confirm screen named the header's city; if the header moved on before
    the manager confirms, refuse and change nothing."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    confirm_cb = FakeCallback("settings_reset_city:start_text")
    asyncio.run(admin_mod.settings_reset_city(confirm_cb))

    # Header moves on before the manager taps "Да, как везде".
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))

    go_cb = FakeCallback("settings_reset_city_go:start_text:spb")
    asyncio.run(admin_mod.settings_reset_city_go(go_cb))

    assert go_cb.answers and go_cb.answers[0][0] == "Город админки изменился — подтвердите заново."
    resolved = asyncio.run(cities.get_setting_for_city("start_text", "spb"))
    assert resolved == "Питерский текст"  # untouched


def test_bound_manager_refused_reset_go_on_forged_other_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "tyumen"), "Тюменский текст"))

    cb = FakeCallback("settings_reset_city_go:start_text:tyumen", user_id=MANAGER_ID)
    asyncio.run(admin_mod.settings_reset_city_go(cb))

    assert cb.answers and cb.answers[0][1] is True
    resolved = asyncio.run(cities.get_setting_for_city("start_text", "tyumen"))
    assert resolved == "Тюменский текст"  # untouched


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 3 / Phase 09.3-05 (CITY-09): group screen relative to the header — «🏙 N» summary now
# lives in «все города» mode only; header = city switches the per-row flag to «своё»/«как
# везде» instead. Same invariants as before the header model, just re-pointed at the mode
# where they actually apply (see plan 05's Task 2 read_first).
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_group_text_byte_identical_when_module_off(tmp_path):
    _admin_ready(tmp_path)
    text_off = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    assert "🏙" not in text_off


def test_group_text_shows_override_count_mark_in_all_cities_mode(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    text_before = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    start_line_before = [ln for ln in text_before.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "🏙" not in start_line_before

    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))
    text_after = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    start_line_after = [ln for ln in text_after.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "🏙 1" in start_line_after

    asyncio.run(db.set_setting(cities.per_city_key("start_text", "tyumen"), "Тюменский текст"))
    text_after2 = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    start_line_after2 = [ln for ln in text_after2.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "🏙 2" in start_line_after2


def test_group_text_flags_unchanged_alongside_mark_in_all_cities_mode(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    asyncio.run(db.set_setting("start_text", "Общий текст"))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))
    text = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    line = [ln for ln in text.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "✏️ задано" in line
    assert "🏙 1" in line


def test_render_settings_group_text_uses_registry_helpers():
    import inspect
    src = inspect.getsource(admin_mod.render_settings_group_text)
    assert "city_override_codes" in src and "cities_module_on" in src


# ── Новые случаи (план 05, CONTEXT B): экран группы относительно шапки = город ─────────────

def test_group_text_header_first_line_at_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    text = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert text.splitlines()[0] == f"🏙 {spb_label}"


def test_group_text_own_mark_when_city_has_override(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    text = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    line = [ln for ln in text.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "✏️ своё" in line
    assert "🏙" not in line  # no override-count suffix on the header-scoped row


def test_group_text_as_everywhere_word_when_no_own_and_global_set(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting("start_text", "Общий текст"))

    text = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    line = [ln for ln in text.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "как везде" in line
    assert "задано" in line
    assert "✏️ своё" not in line


def test_group_text_as_everywhere_default_word_when_no_own_and_no_global(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    # No real per_city key ships with a display default (_SETTINGS_DISPLAY_DEFAULTS) today --
    # exercise the ladder's third rung directly, same as _SETTINGS_DISPLAY_DEFAULTS's own
    # dict shape (registry-derived, not hardcoded here).
    monkeypatch.setitem(admin_mod._SETTINGS_DISPLAY_DEFAULTS, "start_text", "Текст по умолчанию")

    text = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    line = [ln for ln in text.splitlines() if ln.startswith("💬 Приветствие")][0]
    assert "как везде" in line
    assert "по умолчанию" in line


def test_group_text_non_per_city_key_shows_plain_global_flag_at_header_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting("event_name", "Форум"))

    text = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    line = [ln for ln in text.splitlines() if ln.startswith("🎪 Название меро")][0]
    assert "✏️ задано" in line
    assert "своё" not in line and "как везде" not in line


def test_group_keyboard_marks_city_only_override_as_configured(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "spb"), "Питерский текст"))

    kb = asyncio.run(admin_mod.build_settings_group_keyboard("event", ADMIN_ID))
    rows_before_noop = []
    for row in kb.inline_keyboard:
        if any(b.callback_data == "settings_group_noop" for b in row):
            break
        rows_before_noop.append(row)
    configured_texts = [b.text for row in rows_before_noop for b in row]
    assert any("Приветствие" in t for t in configured_texts)


def test_group_text_and_keyboard_module_off_byte_identical_to_admin_id_none(tmp_path):
    """admin_id passed but the cities module is off -> byte-identical to the admin_id=None
    call (module-off collapses admin_selected_city to None regardless of who's asking)."""
    _admin_ready(tmp_path)
    text_none = asyncio.run(admin_mod.render_settings_group_text("event"))
    text_id = asyncio.run(admin_mod.render_settings_group_text("event", ADMIN_ID))
    assert text_none == text_id

    kb_none = asyncio.run(admin_mod.build_settings_group_keyboard("event"))
    kb_id = asyncio.run(admin_mod.build_settings_group_keyboard("event", ADMIN_ID))
    assert [b.callback_data for row in kb_none.inline_keyboard for b in row] == \
        [b.callback_data for row in kb_id.inline_keyboard for b in row]


def test_bound_manager_full_scenario_sees_only_own_city(tmp_path):
    """Phase 09.3-06 (CITY-09): a bound manager's header is LOCKED to their own city
    (cities.admin_selected_city short-circuits to get_staff_city before ever reading
    bot_settings) — the header-scoped editor can structurally never show them another city's
    context, and a forged reset for another city is refused server-side."""
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    codes = asyncio.run(admin_mod._per_city_visible_codes(MANAGER_ID))
    assert codes == ["spb"]

    cb = FakeCallback("settings_edit:start_text", user_id=MANAGER_ID)
    state = FakeState()
    asyncio.run(admin_mod.settings_edit_start(cb, state))
    spb_label = asyncio.run(cities.city_label("spb"))
    msk_label = asyncio.run(cities.city_label("msk"))
    assert cb.message.text.splitlines()[0] == f"🏙 {spb_label}"
    assert msk_label not in cb.message.text

    # Own city: editing succeeds.
    own_cb = FakeCallback("settings_edit_city:start_text", user_id=MANAGER_ID)
    own_state = FakeState()
    asyncio.run(admin_mod.settings_edit_city(own_cb, own_state))
    assert own_state.data.get("setting_key") == cities.per_city_key("start_text", "spb")

    # Forged reset for another city: refused, nothing written (settings_reset_city_go is the
    # only surviving callback that still carries an explicit city code).
    asyncio.run(db.set_setting(cities.per_city_key("start_text", "msk"), "Московский текст"))
    other_cb = FakeCallback("settings_reset_city_go:start_text:msk", user_id=MANAGER_ID)
    asyncio.run(admin_mod.settings_reset_city_go(other_cb))
    assert other_cb.answers and other_cb.answers[0][1] is True
    resolved = asyncio.run(cities.get_setting_for_city("start_text", "msk"))
    assert resolved == "Московский текст"  # untouched
