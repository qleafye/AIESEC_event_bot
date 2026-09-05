"""Phase 25 Plan 04 (CITYQ-04) tests: «📋 Вопросы регистрации» screen relative to the
admin-panel header — the manager-facing surface for 25-01/25-03's per-city question
mechanism. Mirrors tests/test_admin_percity_menu.py's structure/idiom for the sibling
«🔘 Кнопки главного меню» screen.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as tests/test_admin_percity_menu.py.

Sections:
    A — city header: text names the city, marks «своё»/«как везде», no city code anywhere.
    B — toggle_reg_question (full track): composite-key write, isolation from global key,
        bound-manager gate (foreign_city).
    C — toggle_party_question / toggle_short_question (city branch): tri-state cycle vs
        explicit on/off, isolation from global track key.
    D — reg_resume_mode_toggle: composite key, human labels only.
    E — preset_confirm: refuses at a city header, writes nothing.
    F — «↩️ Как везде»: confirm names the count, reg_q_reset_city_go deletes/refuses.
    G — module_off parity: admin_id-vs-None byte-identical output.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_reg_config
from handlers import admin_reg_percity  # module-size split: per-city questions/prompts screens
from handlers.admin_caps import role_caps_key, role_enabled_key
from handlers.reg_schema import REG_FLOW
import cities


ADMIN_ID = 920801
MANAGER_ID = 920802


def _admin_ready(tmp_path, db_name="test_admin_percity_questions_25.db"):
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


def _kb_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


SETTING_KEY = "reg_q_age"  # per_city:True, default "on" (REG_FLOW/SETTINGS_SCHEMA)


# ══════════════════════════════════════════════════════════════════════════════════════════
# A: city header — names the city, marks own/as-everywhere, no city code anywhere
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_questions_text_at_city_header_names_city_and_marks_own_row(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key(SETTING_KEY, "spb"), "off"))

    text = asyncio.run(admin_reg_percity.render_questions_text("full", ADMIN_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in text.splitlines()[0]
    assert "spb" not in text

    from reg_labels import REG_LABELS
    age_label = REG_LABELS[SETTING_KEY]
    age_line = [ln for ln in text.splitlines() if age_label in ln][0]
    assert age_line.startswith("❌")
    assert "(своё)" in age_line

    other_key = "reg_q_vk"  # default "on", no override -> "как везде"
    other_label = REG_LABELS[other_key]
    other_line = [ln for ln in text.splitlines() if other_label in ln][0]
    assert other_line.startswith("✅")
    assert "(как везде)" in other_line
    assert "(своё)" not in other_line


def test_build_questions_keyboard_at_city_header_no_city_code_in_labels(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key(SETTING_KEY, "spb"), "off"))

    kb = asyncio.run(admin_reg_percity.build_questions_keyboard("full", ADMIN_ID))
    texts = _kb_texts(kb)
    assert not any("spb" in t for t in texts)
    from reg_labels import REG_LABELS
    age_text = [t for t in texts if REG_LABELS[SETTING_KEY] in t][0]
    assert age_text.startswith("❌")
    assert "•" in age_text  # own-value bullet marker
    # Toggle callback_data unchanged (reg_q_toggle:{key}), never a per-city-prefixed variant.
    toggle_rows = [row for row in kb.inline_keyboard if row[0].callback_data and row[0].callback_data.startswith("reg_q_toggle:")]
    assert all(":" in row[0].callback_data and row[0].callback_data.split(":", 1)[1] in {sk for _, sk, *_ in REG_FLOW} | {"reg_q_resume"} for row in toggle_rows)


def test_bound_manager_header_locked_to_own_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    text = asyncio.run(admin_reg_percity.render_questions_text("full", MANAGER_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    msk_label = asyncio.run(cities.city_label("msk"))
    assert spb_label in text.splitlines()[0]
    assert msk_label not in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# B: toggle_reg_question (full track) — composite-key write, isolation, foreign_city gate
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_toggle_reg_question_city_writes_composite_key_not_global(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback(f"reg_q_toggle:{SETTING_KEY}")
    asyncio.run(admin_reg_percity.toggle_reg_question(cb))

    assert asyncio.run(db.get_setting(SETTING_KEY)) is None  # global untouched
    assert asyncio.run(db.get_setting(cities.per_city_key(SETTING_KEY, "spb"))) == "off"  # default is "on" -> inverted

    cb2 = FakeCallback(f"reg_q_toggle:{SETTING_KEY}")
    asyncio.run(admin_reg_percity.toggle_reg_question(cb2))
    assert asyncio.run(db.get_setting(cities.per_city_key(SETTING_KEY, "spb"))) == "on"
    assert asyncio.run(db.get_setting(SETTING_KEY)) is None


def test_toggle_reg_question_global_mode_writes_global_key(tmp_path):
    """Module off / no header / «все города» -- unchanged behaviour, writes the plain key."""
    _admin_ready(tmp_path)
    cb = FakeCallback(f"reg_q_toggle:{SETTING_KEY}")
    asyncio.run(admin_reg_percity.toggle_reg_question(cb))
    assert asyncio.run(db.get_setting(SETTING_KEY)) == "off"


def test_toggle_reg_question_all_cities_header_writes_global_key(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback(f"reg_q_toggle:{SETTING_KEY}")
    asyncio.run(admin_reg_percity.toggle_reg_question(cb))
    assert asyncio.run(db.get_setting(SETTING_KEY)) == "off"
    assert asyncio.run(db.get_setting(cities.per_city_key(SETTING_KEY, "spb"))) is None


def test_toggle_reg_question_bound_manager_refused_on_foreign_city(tmp_path, monkeypatch):
    """T-25-12: RIGHT re-checked in the handler itself, not just by hiding the button."""
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    async def _empty_visible(admin_id):
        return []
    monkeypatch.setattr(admin_reg_percity, "_per_city_visible_codes", _empty_visible)

    cb = FakeCallback(f"reg_q_toggle:{SETTING_KEY}", user_id=MANAGER_ID)
    asyncio.run(admin_reg_percity.toggle_reg_question(cb))
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(cities.per_city_key(SETTING_KEY, "spb"))) is None


def test_toggle_reg_question_bound_manager_own_city_works(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    cb = FakeCallback(f"reg_q_toggle:{SETTING_KEY}", user_id=MANAGER_ID)
    asyncio.run(admin_reg_percity.toggle_reg_question(cb))
    assert asyncio.run(db.get_setting(cities.per_city_key(SETTING_KEY, "spb"))) == "off"


def test_toggle_reg_question_city_unknown_key_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("reg_q_toggle:not_a_question_key")
    asyncio.run(admin_reg_percity.toggle_reg_question(cb))
    assert cb.message.edit_calls == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
# C: toggle_party_question / toggle_short_question — city branch
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_toggle_party_question_city_cycle_inherit_on_off_inherit(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    composed = cities.per_city_key(f"{SETTING_KEY}__party", "spb")

    asyncio.run(admin_reg_percity.toggle_party_question(FakeCallback(f"reg_q_ptoggle:{SETTING_KEY}")))
    assert asyncio.run(db.get_setting(composed)) == "on"
    asyncio.run(admin_reg_percity.toggle_party_question(FakeCallback(f"reg_q_ptoggle:{SETTING_KEY}")))
    assert asyncio.run(db.get_setting(composed)) == "off"
    asyncio.run(admin_reg_percity.toggle_party_question(FakeCallback(f"reg_q_ptoggle:{SETTING_KEY}")))
    assert asyncio.run(db.get_setting(composed)) is None  # back to inherit

    # Global __party key never touched by the city-scoped cycle above.
    assert asyncio.run(db.get_setting(f"{SETTING_KEY}__party")) is None


def test_toggle_short_question_city_explicit_on_off_no_delete(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    composed = cities.per_city_key(f"{SETTING_KEY}__short", "spb")

    deleted = []
    real_delete = db.delete_setting

    async def _tracking_delete(key):
        deleted.append(key)
        return await real_delete(key)
    monkeypatch.setattr(admin_reg_percity, "delete_setting", _tracking_delete)

    asyncio.run(admin_reg_percity.toggle_short_question(FakeCallback(f"reg_q_stoggle:{SETTING_KEY}")))
    assert asyncio.run(db.get_setting(composed)) == "on"
    asyncio.run(admin_reg_percity.toggle_short_question(FakeCallback(f"reg_q_stoggle:{SETTING_KEY}")))
    assert asyncio.run(db.get_setting(composed)) == "off"

    assert deleted == []  # 2-state model: never delete_setting
    assert asyncio.run(db.get_setting(f"{SETTING_KEY}__short")) is None  # global untouched


# ══════════════════════════════════════════════════════════════════════════════════════════
# D: reg_resume_mode_toggle — composite key, human labels only
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_resume_mode_toggle_city_writes_composite_key(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    composed = cities.per_city_key("reg_resume_mode", "spb")

    cb = FakeCallback("reg_resume_mode_toggle")
    asyncio.run(admin_reg_percity.reg_resume_mode_toggle(cb))
    assert asyncio.run(db.get_setting(composed)) == "text_only"
    assert asyncio.run(db.get_setting("reg_resume_mode")) is None  # global untouched


def test_resume_mode_toggle_label_has_no_codes(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    kb = asyncio.run(admin_reg_percity.build_questions_keyboard("full", ADMIN_ID))
    resume_row = [row for row in kb.inline_keyboard if row[0].callback_data == "reg_resume_mode_toggle"]
    assert resume_row
    text = resume_row[0][0].text
    assert "file_or_text" not in text and "text_only" not in text
    assert "файл или текст" in text and "только текст" in text


def test_resume_mode_toggle_global_mode(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_resume_mode_toggle")
    asyncio.run(admin_reg_percity.reg_resume_mode_toggle(cb))
    assert asyncio.run(db.get_setting("reg_resume_mode")) == "text_only"


# ══════════════════════════════════════════════════════════════════════════════════════════
# E: preset_confirm at a city header — refuses, writes nothing
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_preset_confirm_refuses_at_city_header_writes_nothing(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("preset_confirm:forum")
    asyncio.run(admin_reg_config.preset_confirm(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[-1][1] is True
    for key in ("reg_q_age", "reg_q_vk", "reg_q_source"):
        assert asyncio.run(db.get_setting(key)) is None
        assert asyncio.run(db.get_setting(cities.per_city_key(key, "spb"))) is None


def test_preset_confirm_applies_at_all_cities_header(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("preset_confirm:forum")
    asyncio.run(admin_reg_config.preset_confirm(cb))
    assert asyncio.run(db.get_setting("reg_q_age")) == "on"


# ══════════════════════════════════════════════════════════════════════════════════════════
# F: «↩️ Как везде» — reg_q_reset_city / reg_q_reset_city_go
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reset_city_button_visible_only_with_override(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    kb_none = asyncio.run(admin_reg_percity.build_questions_keyboard("full", ADMIN_ID))
    assert "reg_q_reset_city" not in _kb_callbacks(kb_none)

    asyncio.run(db.set_setting(cities.per_city_key(SETTING_KEY, "spb"), "off"))
    kb_with = asyncio.run(admin_reg_percity.build_questions_keyboard("full", ADMIN_ID))
    assert "reg_q_reset_city" in _kb_callbacks(kb_with)


def test_reg_q_reset_city_confirm_names_city_and_count(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting(cities.per_city_key(SETTING_KEY, "spb"), "off"))
    asyncio.run(db.set_setting(cities.per_city_key("reg_q_vk", "spb"), "off"))

    cb = FakeCallback("reg_q_reset_city")
    cb.message.reply_markup = asyncio.run(admin_reg_percity.build_questions_keyboard("full", ADMIN_ID))
    asyncio.run(admin_reg_percity.reg_q_reset_city(cb))
    assert "2" in cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text
    callbacks = _kb_callbacks(cb.message.markup)
    assert "reg_q_reset_city_go:spb:full" in callbacks


def test_reg_q_reset_city_go_deletes_only_current_track_keys(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    full_key = cities.per_city_key(SETTING_KEY, "spb")
    party_key = cities.per_city_key(f"{SETTING_KEY}__party", "spb")
    asyncio.run(db.set_setting(full_key, "off"))
    asyncio.run(db.set_setting(party_key, "on"))  # different track -- must survive a "full" reset

    cb = FakeCallback("reg_q_reset_city_go:spb:full")
    asyncio.run(admin_reg_percity.reg_q_reset_city_go(cb))

    assert asyncio.run(db.get_setting(full_key)) is None
    assert asyncio.run(db.get_setting(party_key)) == "on"  # untouched -- different track


def test_reg_q_reset_city_go_forged_foreign_city_refused(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")
    tyumen_key = cities.per_city_key(SETTING_KEY, "tyumen")
    asyncio.run(db.set_setting(tyumen_key, "off"))

    cb = FakeCallback("reg_q_reset_city_go:tyumen:full", user_id=MANAGER_ID)
    asyncio.run(admin_reg_percity.reg_q_reset_city_go(cb))
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(tyumen_key)) == "off"  # untouched


def test_reg_q_reset_city_go_freshness_check_refuses_after_header_changed(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    composed = cities.per_city_key(SETTING_KEY, "spb")
    asyncio.run(db.set_setting(composed, "off"))

    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))  # header moved on before confirming

    cb = FakeCallback("reg_q_reset_city_go:spb:full")
    asyncio.run(admin_reg_percity.reg_q_reset_city_go(cb))
    assert cb.answers and cb.answers[0][0] == "Город админки изменился — подтвердите заново."
    assert asyncio.run(db.get_setting(composed)) == "off"  # untouched


# ══════════════════════════════════════════════════════════════════════════════════════════
# G: module_off parity — admin_id-vs-None byte-identical output
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_questions_screen_module_off_admin_id_parity(tmp_path):
    _admin_ready(tmp_path)
    for track in ("full", "party", "short"):
        text_none = asyncio.run(admin_reg_percity.render_questions_text(track))
        text_admin = asyncio.run(admin_reg_percity.render_questions_text(track, ADMIN_ID))
        assert text_admin == text_none, track

        kb_none = asyncio.run(admin_reg_percity.build_questions_keyboard(track))
        kb_admin = asyncio.run(admin_reg_percity.build_questions_keyboard(track, ADMIN_ID))
        assert _kb_callbacks(kb_admin) == _kb_callbacks(kb_none), track
        assert _kb_texts(kb_admin) == _kb_texts(kb_none), track
