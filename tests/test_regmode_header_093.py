"""Phase 09.3 Plan 04 (CITY-09) tests: settings landing screen relative to the admin-panel
header + registration_mode («форма регистрации») edited per-city through the header, closing
the one per_city key that had no settings_edit screen of its own before the picker (settings_
city:*) is removed by plan 06.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_admin_percity_ui.py / tests/test_city_header_093.py.

Sections mirror the plan's tasks:
    Task 1 -- admin_id plumbing: structural sentinel (no empty-parens calls remain) + the
              admin_id=None / module-off byte-identical contract.
    Task 2 -- header-scoped registration_mode: landing text/keyboard per behavior bullet,
              toggle write path, reset confirm/go two-step gate, rights, freshness, ALL_CITIES
              and module-off parity, ADMIN_CAPS coverage.
"""
import asyncio
import re
from pathlib import Path

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_settings  # Phase 13 (13-06): settings moved out of admin.py
from handlers.admin_caps import required_capability
import cities


ADMIN_ID = 930401
MSK_MANAGER_ID = 930402
SPB_MANAGER_ID = 930403


def _admin_ready(tmp_path, db_name="test_regmode_header_093.db"):
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


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_button_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: admin_id plumbing
# ══════════════════════════════════════════════════════════════════════════════════════════

def _non_comment_source(path):
    """Idiom from tests/test_roles_phase8.py::_non_comment_source -- strips whole-comment
    lines (first non-whitespace char == "#") before substring/regex checks, so an explanatory
    comment can never itself trip or fake-pass a structural gate."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()[:1] != "#"
    )


def test_gate_no_empty_paren_landing_calls_remain():
    """Every production call of render_settings_text()/build_settings_keyboard() in
    handlers/admin.py must pass an admin_id -- the empty-parens form only ever appears in the
    function's OWN default-parameter declaration (`admin_id: int | None = None`), never as a
    call."""
    repo_root = Path(__file__).resolve().parent.parent
    source = _non_comment_source(repo_root / "handlers" / "admin.py")
    assert not re.search(r"render_settings_text\(\)", source)
    assert not re.search(r"build_settings_keyboard\(\)", source)


def test_render_and_build_accept_admin_id_kwarg_module_off_byte_identical(tmp_path):
    """Task 1 contract: admin_id=None (omitted) and a real admin_id, with the cities module
    OFF, must produce byte-identical output -- module-off collapses admin_selected_city to
    None regardless of who's asking."""
    _admin_ready(tmp_path)
    text_none = asyncio.run(admin_settings.render_settings_text())
    text_id = asyncio.run(admin_settings.render_settings_text(ADMIN_ID))
    assert text_none == text_id

    kb_none = asyncio.run(admin_settings.build_settings_keyboard())
    kb_id = asyncio.run(admin_settings.build_settings_keyboard(ADMIN_ID))
    assert _flat_callback_data(kb_none) == _flat_callback_data(kb_id)
    assert _flat_button_texts(kb_none) == _flat_button_texts(kb_id)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: header-scoped registration_mode
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_landing_shows_header_city_first_line_and_as_everywhere_mark(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    text = asyncio.run(admin_settings.render_settings_text(ADMIN_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert text.splitlines()[0] == f"🏙 {spb_label}"
    assert "📝 Форма регистрации: <b>⚡ Краткая</b> — как везде" in text

    kb = asyncio.run(admin_settings.build_settings_keyboard(ADMIN_ID))
    assert "settings_regmode_reset" not in _flat_callback_data(kb)
    # picker shortcut is gone entirely, not just conditionally hidden.
    assert not any(d and d.startswith("settings_city:") for d in _flat_callback_data(kb))


def test_toggle_at_header_city_writes_composite_key_not_global(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_toggle_reg", user_id=ADMIN_ID)
    asyncio.run(admin_settings.toggle_registration_mode(cb))

    assert asyncio.run(db.get_setting("registration_mode__city__spb")) == "full"
    assert asyncio.run(db.get_setting("registration_mode")) is None  # global untouched
    assert cb.answers and cb.answers[0][1] is True
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.answers[0][0]

    # screen re-render shows "своё" + the reset button.
    text = cb.message.text
    assert "— своё" in text
    kb = cb.message.markup
    assert "settings_regmode_reset" in _flat_callback_data(kb)
    reset_texts = [t for t in _flat_button_texts(kb) if t == "↩️ Как везде"]
    assert reset_texts


def test_toggle_at_all_cities_writes_global_key_unchanged_from_before_phase(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))

    cb = FakeCallback("settings_toggle_reg", user_id=ADMIN_ID)
    asyncio.run(admin_settings.toggle_registration_mode(cb))

    assert asyncio.run(db.get_setting("registration_mode")) == "full"
    assert asyncio.run(db.get_setting("registration_mode__city__msk")) is None
    text = cb.message.text
    assert not text.startswith("🏙")
    assert "— своё" not in text and "— как везде" not in text
    kb = cb.message.markup
    assert "settings_regmode_reset" not in _flat_callback_data(kb)


def test_toggle_module_off_byte_identical_to_before_phase(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("settings_toggle_reg", user_id=ADMIN_ID)
    asyncio.run(admin_settings.toggle_registration_mode(cb))

    assert asyncio.run(db.get_setting("registration_mode")) == "full"
    text = cb.message.text
    assert not text.startswith("🏙")
    assert "— своё" not in text and "— как везде" not in text


def test_reset_confirm_screen_names_city_and_global_value(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode", "full"))
    asyncio.run(db.set_setting("registration_mode__city__spb", "short"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_regmode_reset", user_id=ADMIN_ID)
    asyncio.run(admin_settings.settings_regmode_reset(cb))

    text = cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in text
    assert "📋 Полная" in text  # names the GLOBAL value it will fall back to
    kb = cb.message.markup
    assert f"settings_regmode_reset_go:spb" in _flat_callback_data(kb)


def test_reset_confirm_refuses_when_no_own_value(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("settings_regmode_reset", user_id=ADMIN_ID)
    asyncio.run(admin_settings.settings_regmode_reset(cb))

    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True


def test_reset_go_deletes_override_and_is_idempotent(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode__city__spb", "full"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb1 = FakeCallback("settings_regmode_reset_go:spb", user_id=ADMIN_ID)
    asyncio.run(admin_settings.settings_regmode_reset_go(cb1))
    assert asyncio.run(db.get_setting("registration_mode__city__spb")) is None
    assert cb1.answers and cb1.answers[0][1] is True

    # repeat -- idempotent, no error, still gone.
    cb2 = FakeCallback("settings_regmode_reset_go:spb", user_id=ADMIN_ID)
    asyncio.run(admin_settings.settings_regmode_reset_go(cb2))
    assert asyncio.run(db.get_setting("registration_mode__city__spb")) is None


def test_reset_go_context_mismatch_refuses_and_deletes_nothing(tmp_path):
    """T-093-14: the confirm dialog named "spb"; header switched to "msk" before "Да,
    как везде" was tapped -- refuse, don't touch the override."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode__city__spb", "full"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    # header switched AFTER the confirm dialog was shown (stale button, callback_data still
    # names "spb").
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))

    cb = FakeCallback("settings_regmode_reset_go:spb", user_id=ADMIN_ID)
    asyncio.run(admin_settings.settings_regmode_reset_go(cb))

    assert cb.answers and cb.answers[0] == ("Город админки изменился — подтвердите заново.", True)
    assert asyncio.run(db.get_setting("registration_mode__city__spb")) == "full"
    assert cb.message.edit_calls == 1  # re-rendered the landing screen


def test_bound_manager_forged_reset_go_for_other_city_refused(tmp_path):
    """T-093-12: a manager bound to Moscow forges a settings_regmode_reset_go:spb callback --
    admin_selected_city ignores stored choices for a bound manager and always resolves to
    their own city, so this exercises the RIGHT check (_per_city_visible_codes), not the
    freshness check."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.set_setting("registration_mode__city__spb", "full"))

    cb = FakeCallback("settings_regmode_reset_go:spb", user_id=MSK_MANAGER_ID)
    asyncio.run(admin_settings.settings_regmode_reset_go(cb))

    assert cb.answers and cb.answers[0] == ("Этот город правит суперадмин", True)
    assert asyncio.run(db.get_setting("registration_mode__city__spb")) == "full"  # untouched


def test_bound_manager_forged_toggle_only_ever_touches_own_city(tmp_path):
    """toggle_registration_mode has no callback_data code (it's derived purely from the
    header) -- a bound manager's header always resolves to their own bound city, so this
    proves the write lands on THEIR city even if bot_settings once held a stale different
    choice for them."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    # a stale admin_city__ choice from before the binding existed.
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{MSK_MANAGER_ID}", "spb"))

    cb = FakeCallback("settings_toggle_reg", user_id=MSK_MANAGER_ID)
    asyncio.run(admin_settings.toggle_registration_mode(cb))

    assert asyncio.run(db.get_setting("registration_mode__city__msk")) == "full"
    assert asyncio.run(db.get_setting("registration_mode__city__spb")) is None


def test_menu_counter_uses_effective_percity_values_at_header(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    total = len(admin_mod.MENU_BUTTONS)

    text_default = asyncio.run(admin_settings.render_settings_text(ADMIN_ID))
    assert f"🔘 Меню: <b>{total} из {total}</b> кнопок" in text_default  # all default "on"

    off_key = admin_mod.MENU_BUTTONS[0][0]
    asyncio.run(db.set_setting(cities.per_city_key(off_key, "spb"), "off"))
    text_after = asyncio.run(admin_settings.render_settings_text(ADMIN_ID))
    assert f"🔘 Меню: <b>{total - 1} из {total}</b> кнопок" in text_after

    # a msk override must NOT affect the spb-headed counter.
    on_key2 = admin_mod.MENU_BUTTONS[1][0]
    asyncio.run(db.set_setting(cities.per_city_key(on_key2, "msk"), "off"))
    text_still = asyncio.run(admin_settings.render_settings_text(ADMIN_ID))
    assert f"🔘 Меню: <b>{total - 1} из {total}</b> кнопок" in text_still


def test_admin_caps_covers_both_new_callbacks_without_prefix_collision(tmp_path):
    assert required_capability(callback_data="settings_regmode_reset") == "settings"
    assert required_capability(callback_data="settings_regmode_reset_go:spb") == "settings"
    # exact-match "settings_regmode_reset" is a DIFFERENT key from the "settings_regmode_
    # reset_go:*" prefix entry -- a bare "settings_regmode_reset_go" (no trailing city code)
    # deny-by-defaults, proving the two entries don't silently merge into one match.
    assert required_capability(callback_data="settings_regmode_reset_go") is None


def test_module_off_landing_and_toggle_byte_identical_no_percity_surface(tmp_path):
    """Sweep across the specific surfaces this plan touches -- the file-wide contract lives
    in tests/test_content_percity_offparity.py; this is the plan-local companion."""
    _admin_ready(tmp_path)
    # event_city_enabled intentionally left unset (neither "on" nor "off").
    text = asyncio.run(admin_settings.render_settings_text(ADMIN_ID))
    assert not text.startswith("🏙")
    assert "— своё" not in text and "— как везде" not in text

    kb = asyncio.run(admin_settings.build_settings_keyboard(ADMIN_ID))
    assert "settings_regmode_reset" not in _flat_callback_data(kb)
    assert not any(d and d.startswith("settings_city:") for d in _flat_callback_data(kb))
