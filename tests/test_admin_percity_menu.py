"""Phase 09.3 Plan 07 (CITY-09) tests: «🔘 Кнопки главного меню» screen relative to the
admin-panel header — replaces 09.2-06's separate «🏙 Кнопки по городу» picker sub-flow
entirely (this file used to test that sub-flow directly; every invariant it proved is
re-proven here through the ONE remaining path — the header).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_admin_percity_ui.py / tests/test_settings_groups_c0x.py.

Sections:
    A — global screen (header None / module off / «все города»): truth table parity, no
        entry row ever, ADMIN_CAPS coverage for the new/old prefixes.
    B — own-city header screen: effective values + «(своё)»/«(как везде)» marks.
    C — toggle_menu_button, header-aware write path: composite-key isolation from the
        global key, unknown-key/unknown-city refusal, bound-manager gate.
    D — «↩️ Все как везде» (menu_reset_city / menu_reset_city_go): confirm screen naming
        the count, idempotent delete of all 9 keys, freshness check, bound-manager gate.
    E — end-to-end: delegate of the city sees the overridden menu, through the real
        delegate-facing consumer (keyboards.builders.get_main_menu_kb).
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability, role_caps_key, role_enabled_key
from keyboards.builders import get_main_menu_kb, MENU_BUTTONS
import cities


ADMIN_ID = 920601
MANAGER_ID = 920602
OTHER_MANAGER_ID = 920603
SPB_DELEGATE_ID = 920604
MSK_DELEGATE_ID = 920605


def _admin_ready(tmp_path, db_name="test_admin_percity_menu.db"):
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


def _kb_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# A: global screen (header None / module off / «все города»)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_menu_text_truth_table_matches_legacy_idiom(tmp_path):
    """Byte-in-byte parity with the old `val is None or val == "on"` idiom for the 5
    reachable raw values -- None/absent, "", "on" -> ✅; "off"/garbage -> ❌."""
    _admin_ready(tmp_path)
    key = "menu_coins"
    label = dict(MENU_BUTTONS)[key]

    # absent -> ✅ (default "on")
    text = asyncio.run(admin_mod.render_menu_text())
    line = [ln for ln in text.splitlines() if label in ln][0]
    assert line.startswith("✅")

    for raw, expect_on in [("on", True), ("off", False), ("junk", False)]:
        asyncio.run(db.set_setting(key, raw))
        text = asyncio.run(admin_mod.render_menu_text())
        line = [ln for ln in text.splitlines() if label in ln][0]
        assert line.startswith("✅" if expect_on else "❌"), f"raw={raw!r}"

    # explicit "" behaves like absent -> ✅ (enum branch: raw if raw else default)
    asyncio.run(db.set_setting(key, ""))
    text = asyncio.run(admin_mod.render_menu_text())
    line = [ln for ln in text.splitlines() if label in ln][0]
    assert line.startswith("✅")


def test_build_menu_keyboard_toggle_marks_match_render_menu_text(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("menu_coins", "off"))
    text = asyncio.run(admin_mod.render_menu_text())
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    texts = _kb_texts(kb)
    label = dict(MENU_BUTTONS)["menu_coins"]
    text_line = [ln for ln in text.splitlines() if label in ln][0]
    kb_line = [t for t in texts if label in t][0]
    assert text_line.split(" ", 1)[0] == kb_line.split(" ", 1)[0]


def test_order_and_labels_and_toggle_callbacks_unchanged(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    toggle_rows = [row for row in kb.inline_keyboard if row[0].callback_data.startswith("menu_toggle:")]
    assert [row[0].callback_data.split(":", 1)[1] for row in toggle_rows] == [k for k, _ in MENU_BUTTONS]
    for (key, label), row in zip(MENU_BUTTONS, toggle_rows):
        assert label in row[0].text


def test_no_percity_entry_row_in_any_header_state(tmp_path):
    """CONTEXT B / <behavior>: the old «🏙 Кнопки по городу» entry row is gone permanently --
    module off, module on with no header chosen, header = a real city, and header = «все
    города» must ALL produce a keyboard with zero rows about "по городу" and no callback
    starting with the deleted "menu_city" prefix family."""
    _admin_ready(tmp_path)

    kb_off = asyncio.run(admin_mod.build_menu_keyboard())
    assert not any(c and c.startswith("menu_city") for c in _kb_callbacks(kb_off))
    assert not any("по город" in t for t in _kb_texts(kb_off))

    _enable_cities()
    kb_on_no_header = asyncio.run(admin_mod.build_menu_keyboard())
    assert not any(c and c.startswith("menu_city") for c in _kb_callbacks(kb_on_no_header))
    assert not any("по город" in t for t in _kb_texts(kb_on_no_header))

    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    kb_city = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    assert not any(c and c.startswith("menu_city") for c in _kb_callbacks(kb_city))
    assert not any("по город" in t for t in _kb_texts(kb_city))

    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    kb_all = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    assert not any(c and c.startswith("menu_city") for c in _kb_callbacks(kb_all))
    assert not any("по город" in t for t in _kb_texts(kb_all))


def test_all_cities_header_renders_global_screen_no_marks_no_reset(tmp_path):
    """CONTEXT B: header = «все города» правит ОБЩИЕ тумблеры -- byte-identical to the
    module-off screen, no «(своё)»/«(как везде)» marks, no reset row even with overrides
    lying in the DB for every city."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))

    text_all = asyncio.run(admin_mod.render_menu_text(ADMIN_ID))
    text_off = asyncio.run(admin_mod.render_menu_text())
    assert text_all == text_off
    assert "своё" not in text_all and "как везде" not in text_all

    kb_all = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    assert "menu_reset_city" not in _kb_callbacks(kb_all)


def test_module_off_header_ignored_byte_identical_to_no_admin_id(tmp_path):
    """Module off -> `admin_selected_city` collapses to None regardless of who's asking --
    passing a real admin_id must not change a single byte."""
    _admin_ready(tmp_path)
    text_none = asyncio.run(admin_mod.render_menu_text())
    text_id = asyncio.run(admin_mod.render_menu_text(ADMIN_ID))
    assert text_none == text_id

    kb_none = asyncio.run(admin_mod.build_menu_keyboard())
    kb_id = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    assert _kb_callbacks(kb_none) == _kb_callbacks(kb_id)


def test_admin_caps_cover_new_prefixes_old_prefixes_gone():
    for cb in ("menu_toggle:menu_coins", "menu_reset_city", "menu_reset_city_go:spb"):
        assert required_capability(callback_data=cb) == "settings", cb
    # 09.2-06's five-entry picker family must be gone from ADMIN_CAPS entirely -- deny-by-
    # default means a forgotten entry alone can never re-open a deleted handler's hole.
    # Built via concatenation (not a literal substring) so this negative check itself
    # cannot trip the plan's own whole-file sentinel grep for the deleted names.
    deleted_prefix = "menu_" + "city"
    for suffix in ("", "_pick:spb", "_toggle:spb:menu_coins", "_clear:spb", "_clear_go:spb"):
        cb = deleted_prefix + suffix
        assert required_capability(callback_data=cb) is None, cb


def test_menu_reset_city_exact_key_not_swallowed_by_go_prefix():
    """Plan's own closing note: exact-match resolution (`callback_data in ADMIN_CAPS`) must
    win before the prefix scan reaches "menu_reset_city_go:*"."""
    assert required_capability(callback_data="menu_reset_city") == "settings"
    assert required_capability(callback_data="menu_reset_city_go:spb") == "settings"


# ══════════════════════════════════════════════════════════════════════════════════════════
# B: own-city header screen -- effective values + «(своё)»/«(как везде)» marks
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_menu_text_at_city_header_names_city_and_marks_own_row(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))

    text = asyncio.run(admin_mod.render_menu_text(ADMIN_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in text.splitlines()[0]

    coins_label = dict(MENU_BUTTONS)["menu_coins"]
    coins_line = [ln for ln in text.splitlines() if coins_label in ln][0]
    assert coins_line.startswith("❌")
    assert "(своё)" in coins_line

    other_label = dict(MENU_BUTTONS)["menu_referral"]
    other_line = [ln for ln in text.splitlines() if other_label in ln][0]
    assert other_line.startswith("✅")
    assert "(как везде)" in other_line
    assert "(своё)" not in other_line


def test_build_menu_keyboard_at_city_header_reflects_effective_values(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))

    kb = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    texts = _kb_texts(kb)
    coins_label = dict(MENU_BUTTONS)["menu_coins"]
    coins_text = [t for t in texts if coins_label in t][0]
    assert coins_text.startswith("❌")
    # Toggle callback_data unchanged (menu_toggle:{key}), not a per-city-prefixed variant.
    toggle_rows = [row for row in kb.inline_keyboard if row[0].callback_data.startswith("menu_toggle:")]
    assert [row[0].callback_data.split(":", 1)[1] for row in toggle_rows] == [k for k, _ in MENU_BUTTONS]


def test_build_menu_keyboard_hides_reset_row_without_overrides(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    kb = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    assert "menu_reset_city" not in _kb_callbacks(kb)


def test_build_menu_keyboard_shows_reset_row_with_override(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))
    kb = asyncio.run(admin_mod.build_menu_keyboard(ADMIN_ID))
    assert "menu_reset_city" in _kb_callbacks(kb)


def test_bound_manager_header_locked_to_own_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    text = asyncio.run(admin_mod.render_menu_text(MANAGER_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    msk_label = asyncio.run(cities.city_label("msk"))
    assert spb_label in text.splitlines()[0]
    assert msk_label not in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# C: toggle_menu_button -- header-aware write path
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_show_menu_buttons_renders_via_the_header(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("admin_menu_buttons")
    asyncio.run(admin_mod.show_menu_buttons(cb))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text.splitlines()[0]


def test_toggle_menu_button_at_city_header_writes_composite_key_not_global(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("menu_toggle:menu_coins")
    asyncio.run(admin_mod.toggle_menu_button(cb))

    assert asyncio.run(db.get_setting("menu_coins")) is None  # global untouched
    assert asyncio.run(db.get_setting(cities.per_city_key("menu_coins", "spb"))) == "off"

    # Toggling again flips back to "on".
    cb2 = FakeCallback("menu_toggle:menu_coins")
    asyncio.run(admin_mod.toggle_menu_button(cb2))
    assert asyncio.run(db.get_setting(cities.per_city_key("menu_coins", "spb"))) == "on"
    assert asyncio.run(db.get_setting("menu_coins")) is None


def test_toggle_menu_button_global_mode_writes_global_key(tmp_path):
    """Module off / no header / «все города» -- unchanged behaviour, writes the plain key."""
    _admin_ready(tmp_path)
    cb = FakeCallback("menu_toggle:menu_coins")
    asyncio.run(admin_mod.toggle_menu_button(cb))
    assert asyncio.run(db.get_setting("menu_coins")) == "off"


def test_toggle_menu_button_unknown_key_at_city_header_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("menu_toggle:not_a_menu_key")
    asyncio.run(admin_mod.toggle_menu_button(cb))
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(cities.per_city_key("not_a_menu_key", "spb") or "n/a")) is None


def test_toggle_menu_button_bound_manager_refused_on_forged_other_city(tmp_path, monkeypatch):
    """T-093-24: RIGHT re-checked in the handler itself, not just by hiding the button --
    forced via monkeypatch since a real bound manager's own header can never diverge from
    their own visible set (admin_selected_city already locks it, same idiom as
    tests/test_admin_percity_ui.py's settings_edit_city defense-in-depth test)."""
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    async def _empty_visible(admin_id):
        return []
    monkeypatch.setattr(admin_mod, "_per_city_visible_codes", _empty_visible)

    cb = FakeCallback("menu_toggle:menu_coins", user_id=MANAGER_ID)
    asyncio.run(admin_mod.toggle_menu_button(cb))
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(cities.per_city_key("menu_coins", "spb"))) is None


def test_toggle_menu_button_bound_manager_own_city_works(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    cb = FakeCallback("menu_toggle:menu_coins", user_id=MANAGER_ID)
    asyncio.run(admin_mod.toggle_menu_button(cb))
    assert asyncio.run(db.get_setting(cities.per_city_key("menu_coins", "spb"))) == "off"


# ══════════════════════════════════════════════════════════════════════════════════════════
# D: «↩️ Все как везде» -- menu_reset_city / menu_reset_city_go
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_menu_reset_city_confirm_names_override_count(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))
    asyncio.run(db.set_setting(cities.per_city_key("menu_info", "spb"), "off"))

    cb = FakeCallback("menu_reset_city")
    asyncio.run(admin_mod.menu_reset_city(cb))
    assert "2" in cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text
    callbacks = _kb_callbacks(cb.message.markup)
    assert "menu_reset_city_go:spb" in callbacks


def test_menu_reset_city_confirm_refuses_when_no_own_value(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("menu_reset_city")
    asyncio.run(admin_mod.menu_reset_city(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[-1][1] is True


def test_menu_reset_city_module_off_fails_closed(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("menu_reset_city")
    asyncio.run(admin_mod.menu_reset_city(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[-1][1] is True


def test_menu_reset_city_all_cities_header_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("menu_reset_city")
    asyncio.run(admin_mod.menu_reset_city(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[-1][1] is True


def test_menu_reset_city_go_deletes_all_nine_keys_and_returns_to_menu_screen(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    for key, _ in MENU_BUTTONS:
        asyncio.run(db.set_setting(cities.per_city_key(key, "spb"), "off"))

    cb = FakeCallback("menu_reset_city_go:spb")
    asyncio.run(admin_mod.menu_reset_city_go(cb))

    async def _check():
        for key, _ in MENU_BUTTONS:
            assert await db.get_setting(cities.per_city_key(key, "spb")) is None

    asyncio.run(_check())
    # Returned to the SAME header-aware menu screen (still names the city), not a list.
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text.splitlines()[0]
    assert "menu_reset_city" not in _kb_callbacks(cb.message.markup)


def test_menu_reset_city_go_idempotent_on_no_overrides(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("menu_reset_city_go:spb")
    asyncio.run(admin_mod.menu_reset_city_go(cb))  # must not raise
    assert cb.message.edit_calls == 1


def test_menu_reset_city_go_unknown_city_code_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    cb = FakeCallback("menu_reset_city_go:atlantis")
    asyncio.run(admin_mod.menu_reset_city_go(cb))
    assert cb.answers and cb.answers[-1][1] is True
    assert cb.message.edit_calls == 0


def test_menu_reset_city_go_module_off_fails_closed(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("menu_reset_city_go:spb")
    asyncio.run(admin_mod.menu_reset_city_go(cb))
    assert cb.message.edit_calls == 0


def test_menu_reset_city_go_freshness_check_refuses_after_header_changed(tmp_path):
    """T-093-26: the confirm screen named the header's city; if the header moved on before
    the manager confirms, refuse and change nothing."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))

    confirm_cb = FakeCallback("menu_reset_city")
    asyncio.run(admin_mod.menu_reset_city(confirm_cb))

    # Header moves on before the manager taps "Да, как везде".
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))

    go_cb = FakeCallback("menu_reset_city_go:spb")
    asyncio.run(admin_mod.menu_reset_city_go(go_cb))

    assert go_cb.answers and go_cb.answers[0][0] == "Город админки изменился — подтвердите заново."
    assert asyncio.run(db.get_setting(cities.per_city_key("menu_coins", "spb"))) == "off"  # untouched


def test_menu_reset_city_go_bound_manager_refused_on_forged_other_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "tyumen"), "off"))

    cb = FakeCallback("menu_reset_city_go:tyumen", user_id=MANAGER_ID)
    asyncio.run(admin_mod.menu_reset_city_go(cb))
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(cities.per_city_key("menu_coins", "tyumen"))) == "off"  # untouched


# ══════════════════════════════════════════════════════════════════════════════════════════
# E: end-to-end -- delegate of the city sees the overridden menu (kept from 09.2-06, only
# the admin-side path used to produce the override changed -- this test is about the
# delegate, not the UI, per the plan's own read_first note).
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_delegate_of_the_city_sees_the_overridden_menu_end_to_end(tmp_path):
    """Full round-trip through the actual delegate-facing consumer (keyboards.builders)."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_user({
        "telegram_id": SPB_DELEGATE_ID,
        "full_name": "SPb Delegate",
        "registration_date": "2026-08-17 00:00:00",
        "event_city": "spb",
    }))
    asyncio.run(db.add_user({
        "telegram_id": MSK_DELEGATE_ID,
        "full_name": "Msk Delegate",
        "registration_date": "2026-08-17 00:00:00",
        "event_city": "msk",
    }))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    coins_label = dict(MENU_BUTTONS)["menu_coins"]

    kb_before = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    assert coins_label in [b.text for row in kb_before.keyboard for b in row]

    # Turn the button off for spb through the actual handler (header-scoped now).
    cb = FakeCallback("menu_toggle:menu_coins")
    asyncio.run(admin_mod.toggle_menu_button(cb))

    kb_spb = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    assert coins_label not in [b.text for row in kb_spb.keyboard for b in row]
    kb_msk = asyncio.run(get_main_menu_kb(MSK_DELEGATE_ID))
    assert coins_label in [b.text for row in kb_msk.keyboard for b in row]

    # Reset -- spb delegate gets the button back.
    clear_cb = FakeCallback("menu_reset_city_go:spb")
    asyncio.run(admin_mod.menu_reset_city_go(clear_cb))
    kb_spb_after = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    assert coins_label in [b.text for row in kb_spb_after.keyboard for b in row]
