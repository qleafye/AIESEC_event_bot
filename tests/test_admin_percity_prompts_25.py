"""Phase 25 Plan 05 (CITYQ-05) tests: «✏️ Тексты вопросов» screen relative to the admin-panel
header — the manager-facing surface for writing/reading `reg_prompt_*__city__{code}` composite
text overrides. Mirrors tests/test_admin_percity_questions_25.py's structure/idiom for the
sibling «📋 Вопросы регистрации» screen, plus tests/test_admin_percity_ui.py's settings_edit_value
round-trip idiom (this screen reuses that SAME shared FSM handler).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as the sibling test files.

Sections:
    A — city header: text names the city, marks own/as-everywhere, no city code anywhere.
    B — reg_prompt_edit (full track): FSM carries the composite key, own/global lines shown.
    C — reg_prompt_edit (party track): composite key survives cities.split_per_city_key
        (T-25 order-of-suffixes guard on a LIVE key, not just the primitive unit test).
    D — settings_edit_value round-trip through the composite key: write/read/clear, global
        untouched.
    E — bound manager: foreign_city refused on entry AND on the forged reset-go callback.
    F — «↩️ Как везде»: confirm names city+question, freshness check refuses a stale header.
    G — module_off / no-admin_id parity: byte-identical render, and reg_prompt_edit falls
        back to writing the BARE global key.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_reg_percity  # module-size split: per-city questions/prompts screens
from handlers import admin_settings
from handlers.admin_caps import required_capability, role_caps_key, role_enabled_key
import cities


ADMIN_ID = 920901
MANAGER_ID = 920902

STEP_KEY = "expectations"  # reg_prompt_expectations -- has a REG_FLOW toggle sibling (reg_q_expectations)
BASE_KEY = "reg_prompt_expectations"
PARTY_BASE_KEY = "reg_prompt_expectations__party"


def _admin_ready(tmp_path, db_name="test_admin_percity_prompts_25.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


def _add_bound_manager(manager_id=MANAGER_ID, city="spb"):
    asyncio.run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(manager_id, city))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg;moderate_receipts;settings"))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.reply_markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.reply_markup = reply_markup  # real aiogram Message carries the CURRENT keyboard
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

    async def get_state(self):
        return getattr(self.state, "state", self.state)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_data(self, data):
        self.data = dict(data)

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


def _kb_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# A: city header — names the city, marks own/as-everywhere, no city code anywhere
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_prompts_text_at_city_header_names_city_no_code(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    text = asyncio.run(admin_reg_percity.render_prompts_text("full", ADMIN_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in text.splitlines()[0]
    assert "spb" not in text


def test_build_prompts_keyboard_marks_own_vs_common(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key(BASE_KEY, "spb"), "Свой текст ожиданий"))

    kb = asyncio.run(admin_reg_percity.build_prompts_keyboard("full", ADMIN_ID))
    texts = _kb_texts(kb)
    assert not any("spb" in t for t in texts)

    from handlers.reg_schema import REG_LABELS
    label = REG_LABELS["reg_q_expectations"]
    own_text = [t for t in texts if label in t][0]
    assert own_text.startswith("✅")

    # A different step with no override still shows the "common" mark.
    other_label = REG_LABELS["reg_q_vk"]
    other_text = [t for t in texts if other_label in t][0]
    assert other_text.startswith("✏️")

    # Callback data is unchanged regardless of header (no city code anywhere).
    callbacks = _kb_callbacks(kb)
    assert f"reg_prompt_edit:{STEP_KEY}" in callbacks


def test_bound_manager_header_locked_to_own_city_prompts_screen(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    text = asyncio.run(admin_reg_percity.render_prompts_text("full", MANAGER_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    msk_label = asyncio.run(cities.city_label("msk"))
    assert spb_label in text.splitlines()[0]
    assert msk_label not in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# B: reg_prompt_edit (full track) — FSM carries the composite key, own/global lines shown
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_prompt_edit_city_fsm_has_composite_key(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert state.data.get("setting_key") == "reg_prompt_expectations__city__spb"
    assert state.state == admin_mod.EditSetting.waiting_for_value


def test_reg_prompt_edit_city_shows_own_and_common_lines(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(BASE_KEY, "Общий текст ожиданий"))
    asyncio.run(db.set_setting(cities.per_city_key(BASE_KEY, "spb"), "Питерский текст ожиданий"))

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert "Питерский текст ожиданий" in cb.message.text
    assert "Общий текст ожиданий" in cb.message.text
    codes = cities.city_codes()
    assert not any(code in cb.message.text for code in codes)
    # Reset button only appears when the city HAS its own text.
    assert "reg_prompt_rst:expectations" in _kb_callbacks(cb.message.markup)


def test_reg_prompt_edit_city_no_own_value_shows_no_reset_button(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert "как везде" in cb.message.text
    assert "стандартный (по умолчанию)" in cb.message.text
    assert not any(cd and cd.startswith("reg_prompt_rst:") for cd in _kb_callbacks(cb.message.markup))


def test_reg_prompt_edit_city_unknown_step_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("reg_prompt_edit:not_a_real_step")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert cb.message.edit_calls == 0
    assert state.data == {}


# ══════════════════════════════════════════════════════════════════════════════════════════
# C: reg_prompt_edit (party track) — composite key survives cities.split_per_city_key
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_prompt_edit_party_city_key_splits_correctly(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}:party")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    composed = state.data.get("setting_key")
    assert composed == "reg_prompt_expectations__party__city__spb"
    # T-25 order-of-suffixes guard on a LIVE key -- not just cities.py's own primitive unit test.
    parsed = cities.split_per_city_key(composed)
    assert parsed == (PARTY_BASE_KEY, "spb")


def test_reg_prompt_edit_party_city_key_isolated_from_full_track(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    full_cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    full_state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(full_cb, full_state))
    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("Общий per-city текст"), full_state))

    party_cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}:party")
    party_state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(party_cb, party_state))
    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("Party-текст города"), party_state))

    assert asyncio.run(db.get_setting(cities.per_city_key(BASE_KEY, "spb"))) == "Общий per-city текст"
    assert asyncio.run(db.get_setting(cities.per_city_key(PARTY_BASE_KEY, "spb"))) == "Party-текст города"


# ══════════════════════════════════════════════════════════════════════════════════════════
# D: settings_edit_value round-trip through the composite key
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_write_via_settings_edit_value_writes_city_key_not_global(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(BASE_KEY, "Глобальный текст"))

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("Питерский текст"), state))

    assert asyncio.run(db.get_setting(BASE_KEY)) == "Глобальный текст"
    resolved = asyncio.run(cities.get_setting_for_city(BASE_KEY, "spb"))
    assert resolved == "Питерский текст"
    assert state.data == {} and state.state is None


def test_dash_clears_city_key_leaves_global_untouched(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(BASE_KEY, "Глобальный текст"))
    asyncio.run(db.set_setting(cities.per_city_key(BASE_KEY, "spb"), "Питерский текст"))

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))
    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("-"), state))

    assert asyncio.run(db.get_setting(cities.per_city_key(BASE_KEY, "spb"))) is None
    assert asyncio.run(db.get_setting(BASE_KEY)) == "Глобальный текст"


# ══════════════════════════════════════════════════════════════════════════════════════════
# E: bound manager — foreign_city refused on entry AND on the forged reset-go callback
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_prompt_edit_bound_manager_refused_on_foreign_city(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    async def _empty_visible(admin_id):
        return []
    monkeypatch.setattr(admin_reg_percity, "_per_city_visible_codes", _empty_visible)

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}", user_id=MANAGER_ID)
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert cb.message.edit_calls == 0
    assert state.data == {}


def test_reg_prompt_rst_go_forged_foreign_city_refused_no_write(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")
    tyumen_key = cities.per_city_key(BASE_KEY, "tyumen")
    asyncio.run(db.set_setting(tyumen_key, "Тюменский текст"))

    cb = FakeCallback(f"reg_prompt_rst_go:tyumen:{STEP_KEY}", user_id=MANAGER_ID)
    asyncio.run(admin_reg_percity.reg_prompt_rst_go(cb))

    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(tyumen_key)) == "Тюменский текст"  # untouched


def test_reg_prompt_edit_bound_manager_own_city_works(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}", user_id=MANAGER_ID)
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert state.data.get("setting_key") == cities.per_city_key(BASE_KEY, "spb")


# ══════════════════════════════════════════════════════════════════════════════════════════
# F: «↩️ Как везде» — reg_prompt_rst / reg_prompt_rst_go
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_prompt_rst_confirm_names_city_and_question(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key(BASE_KEY, "spb"), "Питерский текст"))

    cb = FakeCallback(f"reg_prompt_rst:{STEP_KEY}")
    asyncio.run(admin_reg_percity.reg_prompt_rst(cb))

    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text
    from handlers.reg_schema import REG_LABELS
    assert REG_LABELS["reg_q_expectations"] in cb.message.text
    callbacks = _kb_callbacks(cb.message.markup)
    assert f"reg_prompt_rst_go:spb:{STEP_KEY}" in callbacks
    assert f"reg_prompt_edit:{STEP_KEY}" in callbacks  # cancel goes back to the edit screen


def test_reg_prompt_rst_refuses_when_no_own_text(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback(f"reg_prompt_rst:{STEP_KEY}")
    asyncio.run(admin_reg_percity.reg_prompt_rst(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[-1][1] is True


def test_reg_prompt_rst_go_deletes_city_key_idempotent(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    composed = cities.per_city_key(BASE_KEY, "spb")
    asyncio.run(db.set_setting(composed, "Питерский текст"))

    cb = FakeCallback(f"reg_prompt_rst_go:spb:{STEP_KEY}")
    asyncio.run(admin_reg_percity.reg_prompt_rst_go(cb))
    assert asyncio.run(db.get_setting(composed)) is None

    # Second tap (idempotent, key already gone) must not raise.
    cb2 = FakeCallback(f"reg_prompt_rst_go:spb:{STEP_KEY}")
    asyncio.run(admin_reg_percity.reg_prompt_rst_go(cb2))
    assert asyncio.run(db.get_setting(composed)) is None


def test_reg_prompt_rst_go_freshness_check_refuses_after_header_changed(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    composed = cities.per_city_key(BASE_KEY, "spb")
    asyncio.run(db.set_setting(composed, "Питерский текст"))

    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))  # header moved on before confirming

    cb = FakeCallback(f"reg_prompt_rst_go:spb:{STEP_KEY}")
    asyncio.run(admin_reg_percity.reg_prompt_rst_go(cb))
    assert cb.answers and cb.answers[0][0] == "Город админки изменился — подтвердите заново."
    assert asyncio.run(db.get_setting(composed)) == "Питерский текст"  # untouched


def test_reg_prompt_rst_go_party_track_isolated_from_full(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    full_key = cities.per_city_key(BASE_KEY, "spb")
    party_key = cities.per_city_key(PARTY_BASE_KEY, "spb")
    asyncio.run(db.set_setting(full_key, "Общий per-city текст"))
    asyncio.run(db.set_setting(party_key, "Party-текст города"))

    cb = FakeCallback(f"reg_prompt_rst_go:spb:{STEP_KEY}:party")
    asyncio.run(admin_reg_percity.reg_prompt_rst_go(cb))

    assert asyncio.run(db.get_setting(party_key)) is None
    assert asyncio.run(db.get_setting(full_key)) == "Общий per-city текст"  # different track, untouched


# ══════════════════════════════════════════════════════════════════════════════════════════
# G: module_off / no-admin_id parity — byte-identical render + edit falls back to global key
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_prompts_screen_module_off_admin_id_parity(tmp_path):
    _admin_ready(tmp_path)
    for track in ("full", "party"):
        text_none = asyncio.run(admin_reg_percity.render_prompts_text(track))
        text_admin = asyncio.run(admin_reg_percity.render_prompts_text(track, ADMIN_ID))
        assert text_admin == text_none, track

        kb_none = asyncio.run(admin_reg_percity.build_prompts_keyboard(track))
        kb_admin = asyncio.run(admin_reg_percity.build_prompts_keyboard(track, ADMIN_ID))
        assert _kb_callbacks(kb_admin) == _kb_callbacks(kb_none), track
        assert _kb_texts(kb_admin) == _kb_texts(kb_none), track


def test_reg_prompt_edit_module_off_writes_bare_global_key(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert state.data.get("setting_key") == BASE_KEY  # bare key, no composite


def test_reg_prompt_edit_all_cities_header_writes_bare_global_key(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback(f"reg_prompt_edit:{STEP_KEY}")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_prompt_edit(cb, state))

    assert state.data.get("setting_key") == BASE_KEY


# ══════════════════════════════════════════════════════════════════════════════════════════
# ADMIN_CAPS coverage
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_admin_caps_covers_new_reset_callbacks():
    assert required_capability(callback_data=f"reg_prompt_rst:{STEP_KEY}") == "settings"
    assert required_capability(callback_data=f"reg_prompt_rst:{STEP_KEY}:party") == "settings"
    assert required_capability(callback_data=f"reg_prompt_rst_go:spb:{STEP_KEY}") == "settings"
    assert required_capability(callback_data=f"reg_prompt_rst_go:spb:{STEP_KEY}:party") == "settings"
    # Distinct literal prefixes -- neither swallows the other (unlike reg_q_reset_city's bare
    # exact-vs-prefix hazard).
    assert required_capability(callback_data=f"reg_prompt_edit:{STEP_KEY}") == "settings"
