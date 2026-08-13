"""Phase 5 Plan 3 (admin UI) tests: tri-state party toggle, track switcher, 🎉 Party
preset routing, party module/approval toggles, track line on the moderation card, and
the participant_type broadcast filter whitelist.

pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async
helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability
from handlers.registration import REG_FLOW, REG_PRESETS


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


ADMIN_ID = 900001


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_admin5.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Stand-in for the aiogram Message the callback carries — captures edit_text calls
    so tests can assert the SAME message is re-rendered (no new message sent)."""

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


# ── Task 1: pure tri-state helpers (D-04) ─────────────────────────────────────

def test_party_tri_state_advance_cycle():
    assert admin_mod._party_tri_state_advance(None) == "on"
    assert admin_mod._party_tri_state_advance("on") == "off"
    assert admin_mod._party_tri_state_advance("off") is None


def test_party_tri_state_label():
    assert admin_mod._party_tri_state_label(None) == "➕ Наследует"
    assert admin_mod._party_tri_state_label("on") == "✅ Вкл"
    assert admin_mod._party_tri_state_label("off") == "❌ Выкл"


# ── Task 1: build_questions_keyboard track param ──────────────────────────────

def test_full_track_keyboard_emits_reg_q_toggle(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_questions_keyboard("full"))
    flat = _flat_callback_data(kb)
    assert any(cd.startswith("reg_q_toggle:") for cd in flat)
    assert not any(cd.startswith("reg_q_ptoggle:") for cd in flat)
    assert "reg_q_track:full" in flat
    assert "reg_q_track:party" in flat


def test_party_track_keyboard_emits_reg_q_ptoggle(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_questions_keyboard("party"))
    flat = _flat_callback_data(kb)
    assert any(cd.startswith("reg_q_ptoggle:") for cd in flat)
    assert not any(cd.startswith("reg_q_toggle:") for cd in flat)
    assert "reg_q_track:full" in flat
    assert "reg_q_track:party" in flat


def test_party_track_text_shows_tri_state_labels(tmp_path):
    _admin_ready(tmp_path)
    setting_key = REG_FLOW[0][1]
    asyncio.run(db.set_setting(f"{setting_key}__party", "on"))
    text = asyncio.run(admin_mod.render_questions_text("party"))
    assert "✅ Вкл" in text or "➕ Наследует" in text  # at least one tri-state marker present


# ── Task 1: reg_q_track: switcher handler (D-06) — re-renders SAME message ────

def test_reg_q_track_switch_reuses_same_message(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_q_track:party")
    asyncio.run(admin_mod.reg_q_track_switch(cb))
    assert cb.message.edit_calls == 1
    assert "Party" in cb.message.text or "🎉" in cb.message.text
    flat = _flat_callback_data(cb.message.markup)
    assert any(cd.startswith("reg_q_ptoggle:") for cd in flat)


def test_reg_q_track_switch_is_capability_guarded():
    # Phase 8 / D-01: the old per-handler `config.ADMIN_IDS` check (and the direct-call test
    # that exercised it) is gone (08-04, one-shot migration, D-03) -- CapabilityMiddleware is
    # now the ONLY enforcement point, and it only wraps events dispatched through the real
    # router, not direct handler calls. The structural guarantee survives with a new carrier:
    # the handler stays registered, and its callback_data resolves to a real capability.
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "reg_q_track_switch" in names
    assert required_capability(callback_data="reg_q_track:party") == "settings"


# ── Task 1: reg_q_ptoggle: tri-state cycle handler (D-04) ─────────────────────

def test_reg_q_ptoggle_cycles_inherit_on_off_inherit(tmp_path):
    _admin_ready(tmp_path)
    setting_key = REG_FLOW[0][1]
    party_key = f"{setting_key}__party"

    asyncio.run(admin_mod.toggle_party_question(FakeCallback(f"reg_q_ptoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(party_key)) == "on"

    asyncio.run(admin_mod.toggle_party_question(FakeCallback(f"reg_q_ptoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(party_key)) == "off"

    asyncio.run(admin_mod.toggle_party_question(FakeCallback(f"reg_q_ptoggle:{setting_key}")))
    assert asyncio.run(db.get_setting(party_key)) is None  # back to inherit (key absent)


def test_reg_q_ptoggle_rejects_unknown_setting_key(tmp_path):
    """T-05-03-02: an unvalidated setting_key must never reach set_setting/delete_setting."""
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_q_ptoggle:not_a_real_step; DROP TABLE users")
    asyncio.run(admin_mod.toggle_party_question(cb))
    assert asyncio.run(db.get_setting("not_a_real_step; DROP TABLE users__party")) is None
    assert cb.answers and cb.answers[0][1] is True


def test_reg_q_ptoggle_is_capability_guarded():
    # Phase 8 / D-01: carrier of the structural guarantee moved from a per-handler inline
    # check to a resolvable ADMIN_CAPS entry (08-04, D-03). See test_reg_q_track_switch_is_
    # capability_guarded above for the full rationale.
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "toggle_party_question" in names
    setting_key = REG_FLOW[0][1]
    assert required_capability(callback_data=f"reg_q_ptoggle:{setting_key}") == "settings"


# ── Task 1: 🎉 Party preset button auto-generated + routed correctly (D-07) ───

def test_preset_keyboard_has_party_button(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("admin_event_preset")
    asyncio.run(admin_mod.admin_event_preset(cb))
    flat = _flat_callback_data(cb.message.markup)
    assert "preset_apply:party" in flat


def test_preset_apply_party_no_keyerror_and_no_raw_settings_key(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("preset_apply:party")
    asyncio.run(admin_mod.preset_apply(cb))  # must not raise (D-07 KeyError fix)
    assert cb.message.text is not None
    assert "reg_q_" not in cb.message.text  # only human labels, never raw setting keys


def test_preset_apply_forum_still_shows_payment_line(tmp_path):
    """Regression: the forum/conf presets still carry payment_enabled and must keep
    surfacing the "Модуль оплаты ..." sentence."""
    _admin_ready(tmp_path)
    cb = FakeCallback("preset_apply:forum")
    asyncio.run(admin_mod.preset_apply(cb))
    assert "Модуль оплаты" in cb.message.text


def test_preset_confirm_party_routes_to_apply_party_preset(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    calls = {"party": 0, "event": 0}

    async def fake_party():
        calls["party"] += 1

    async def fake_event(key):
        calls["event"] += 1

    monkeypatch.setattr(admin_mod, "_apply_party_preset", fake_party)
    monkeypatch.setattr(admin_mod, "_apply_event_preset", fake_event)

    cb = FakeCallback("preset_confirm:party")
    asyncio.run(admin_mod.preset_confirm(cb))
    assert calls == {"party": 1, "event": 0}


def test_preset_confirm_party_leaves_global_reg_q_untouched(tmp_path):
    """D-07: applying the party preset must never write a bare reg_q_* global key. WR-03:
    the overnight-only trio (housing/bed_sharing/bed_partner) is exempt from the explicit
    on/off __party write — it stays at inherit so D-08's skip rule keeps governing it."""
    from handlers.registration import _PARTY_PRESET_OVERNIGHT_EXEMPT
    _admin_ready(tmp_path)
    cb = FakeCallback("preset_confirm:party")
    asyncio.run(admin_mod.preset_confirm(cb))
    for _step_key, setting_key, *_rest in REG_FLOW:
        assert asyncio.run(db.get_setting(setting_key)) is None
        if setting_key in _PARTY_PRESET_OVERNIGHT_EXEMPT:
            assert asyncio.run(db.get_setting(f"{setting_key}__party")) is None
        else:
            assert asyncio.run(db.get_setting(f"{setting_key}__party")) in ("on", "off")
    assert cb.message.edit_calls == 1  # re-rendered the SAME message, once


def test_preset_confirm_forum_still_applies_globally(tmp_path):
    """Regression: forum/conf presets keep writing GLOBAL reg_q_* keys unchanged."""
    _admin_ready(tmp_path)
    cb = FakeCallback("preset_confirm:forum")
    asyncio.run(admin_mod.preset_confirm(cb))
    on_set = set(REG_PRESETS["forum"]["on"])
    for key in on_set:
        assert asyncio.run(db.get_setting(key)) == "on"


# ── Task 2: party_enabled / party_fork_question / party_approval defaults (D-13) ──

def test_party_settings_resolve_to_safe_defaults_when_unset(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    assert (asyncio.run(db.get_setting("party_enabled")) or "off") == "off"
    assert (asyncio.run(db.get_setting("party_fork_question")) or "off") == "off"
    assert (asyncio.run(db.get_setting("party_approval")) or "manual") == "manual"


def test_toggle_party_enabled_flips_off_to_on(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("toggle_party_enabled")
    asyncio.run(admin_mod.toggle_party_enabled(cb))
    assert asyncio.run(db.get_setting("party_enabled")) == "on"
    asyncio.run(admin_mod.toggle_party_enabled(FakeCallback("toggle_party_enabled")))
    assert asyncio.run(db.get_setting("party_enabled")) == "off"


def test_toggle_party_fork_question_flips_off_to_on(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(admin_mod.toggle_party_fork_question(FakeCallback("toggle_party_fork_question")))
    assert asyncio.run(db.get_setting("party_fork_question")) == "on"


def test_toggle_party_approval_is_independent_of_full_and_short(tmp_path):
    """D-13: party_approval never reads/derives from full_approval/short_approval."""
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("full_approval", "auto"))
    asyncio.run(db.set_setting("short_approval", "auto"))
    asyncio.run(admin_mod.toggle_party_approval(FakeCallback("settings_toggle_party_approval")))
    assert asyncio.run(db.get_setting("party_approval")) == "auto"
    # full/short untouched by the party toggle
    assert asyncio.run(db.get_setting("full_approval")) == "auto"
    assert asyncio.run(db.get_setting("short_approval")) == "auto"


def test_party_closed_text_shows_hardcoded_default_when_unset(tmp_path):
    """Quick 260724-c0x: the landing screen no longer dumps field values/defaults inline
    (moved to the «party» group sub-screen, which shows a «по умолчанию» flag, not the
    literal default text — see req: long values never shown inline)."""
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_settings_text())
    assert "Регистрация на вечеринку сейчас закрыта." not in text

    group_text = asyncio.run(admin_mod.render_settings_group_text("party"))
    assert "по умолчанию" in group_text


def test_party_sheet_tab_shows_default_party_when_unset(tmp_path):
    """Quick 260724-c0x: same relocation as above — landing has no inline defaults;
    the party group sub-screen flags the field as «по умолчанию» instead."""
    _admin_ready(tmp_path)
    group_text = asyncio.run(admin_mod.render_settings_group_text("party"))
    assert "party_sheet_tab" in admin_mod._settings_group_keys("party")
    assert "по умолчанию" in group_text


def test_party_closed_text_and_sheet_tab_are_settings_edit_fields(tmp_path):
    keys = {k for k, _, _ in admin_mod.SETTINGS_FIELDS}
    assert "party_closed_text" in keys
    assert "party_sheet_tab" in keys


# ── Task 3: track line on the shared moderation card (D-14) ──────────────────

def test_card_byte_identical_for_full_track():
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "full"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "Трек" not in out


def test_card_byte_identical_when_participant_type_missing():
    u = {"telegram_id": 1, "full_name": "Иван"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "Трек" not in out


def test_card_shows_overnight_track_line():
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "party_overnight"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "🎉 Трек: вечеринка с ночёвкой" in out


def test_card_shows_noovernight_track_line():
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "party_noovernight"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "🎉 Трек: вечеринка без ночёвки" in out


def test_card_escapes_unrecognised_track_value():
    u = {"telegram_id": 1, "full_name": "Иван", "participant_type": "<b>hack</b>"}
    out = admin_mod._render_application_card(u, 1, 1)
    assert "<b>hack</b>" not in out
    assert "&lt;b&gt;hack" in out


# ── Task 3: participant_type broadcast filter whitelist (D-19) ───────────────

def test_filter_columns_and_picker_fields_whitelist_participant_type():
    assert "participant_type" in db._FILTER_COLUMNS
    assert "participant_type" in admin_mod._PICKER_FIELDS
    assert admin_mod._FILTER_FIELD_LABELS["participant_type"] == "Трек"


def test_filter_clause_accepts_participant_type():
    where, params = db._build_filter_clause([{"field": "participant_type", "value": "party_overnight"}])
    assert where == " WHERE participant_type = ?"
    assert params == ["party_overnight"]


def test_get_distinct_filter_values_returns_all_tracks(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_user({
        "telegram_id": 1, "full_name": "A", "registration_date": "2026-01-01",
        "participant_type": "full",
    }))
    asyncio.run(db.add_user({
        "telegram_id": 2, "full_name": "B", "registration_date": "2026-01-01",
        "participant_type": "party_overnight",
    }))
    asyncio.run(db.add_user({
        "telegram_id": 3, "full_name": "C", "registration_date": "2026-01-01",
        "participant_type": "party_noovernight",
    }))
    values = asyncio.run(db.get_distinct_filter_values("participant_type"))
    assert set(values) == {"full", "party_overnight", "party_noovernight"}


def test_filter_menu_kb_has_track_button():
    kb = admin_mod._filter_menu_kb([])
    flat = _flat_callback_data(kb)
    assert "filter_f_participant_type" in flat


# ── WR-01 regression: rcpt_confirm resolves participant_type before completion ──
#
# rcpt_confirm is the manager "confirm receipt" action — the primary PAID party path (a
# party delegate who paid, plan 05-05's whole reason for existing). Pre-fix it called
# send_completion_and_bonus with no track resolution at all, so a paying party delegate
# always got the full-track approve_text.

def test_rcpt_confirm_resolves_party_track_before_completion(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    uid = 800001
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": "Party Payer", "registration_date": "2026-07-01",
        "participant_type": "party_overnight", "payment_status": "receipt_sent",
    }))

    import services.scheduler as scheduler_mod
    monkeypatch.setattr(scheduler_mod, "cancel_payment_reminders", lambda user_id: None)

    sent = {}

    async def fake_completion(bot, telegram_id, with_menu=True, participant_type=None):
        sent["telegram_id"] = telegram_id
        sent["with_menu"] = with_menu
        sent["participant_type"] = participant_type

    monkeypatch.setattr("handlers.registration.send_completion_and_bonus", fake_completion)

    async def fake_show_current_receipt_card(target, state):
        pass

    monkeypatch.setattr(admin_mod, "_show_current_receipt_card", fake_show_current_receipt_card)

    class FakeBot:
        async def send_message(self, *a, **k):
            pass

    class FakeCallback:
        data = f"rcpt_confirm:{uid}"
        from_user = FakeUser(ADMIN_ID)
        bot = FakeBot()
        message = FakeMessage()

        async def answer(self, text=None, show_alert=False):
            pass

    class FakeState:
        pass

    asyncio.run(admin_mod.rcpt_confirm(FakeCallback(), FakeState()))

    assert sent["telegram_id"] == uid
    assert sent["with_menu"] is False
    assert sent["participant_type"] == "party_overnight"


# ── Quick 260724-cfn Task 1: approve_text__party editor + payment_options help (WR-02a, WR-05) ──

def test_approve_text_party_is_settings_edit_field_html_and_in_party_group():
    keys = {k for k, _, _ in admin_mod.SETTINGS_FIELDS}
    assert "approve_text__party" in keys
    assert "approve_text__party" in admin_mod.HTML_SETTINGS
    assert "approve_text__party" in admin_mod._settings_group_keys("party")


def test_payment_options_help_describes_track_filter():
    prompts = {k: prompt for k, _, prompt in admin_mod.SETTINGS_FIELDS}
    help_text = prompts["payment_options"]
    assert "party_overnight" in help_text
    assert "party_noovernight" in help_text


# ── Quick 260724-cfn Task 2: track switcher on «Тексты вопросов» screen (WR-02b) ──

def test_build_prompts_keyboard_full_emits_unsuffixed_callbacks(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_prompts_keyboard("full"))
    flat = _flat_callback_data(kb)
    assert "reg_prompt_track:full" in flat
    assert "reg_prompt_track:party" in flat
    edit_callbacks = [cd for cd in flat if cd.startswith("reg_prompt_edit:")]
    assert edit_callbacks  # at least one step present
    assert all(not cd.endswith(":party") for cd in edit_callbacks)


def test_build_prompts_keyboard_party_emits_party_suffixed_callbacks(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_prompts_keyboard("party"))
    flat = _flat_callback_data(kb)
    assert "reg_prompt_track:full" in flat
    assert "reg_prompt_track:party" in flat
    edit_callbacks = [cd for cd in flat if cd.startswith("reg_prompt_edit:")]
    assert edit_callbacks
    assert all(cd.endswith(":party") for cd in edit_callbacks)


def test_reg_prompt_track_switch_reuses_same_message(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_prompt_track:party")
    asyncio.run(admin_mod.reg_prompt_track_switch(cb))
    assert cb.message.edit_calls == 1
    flat = _flat_callback_data(cb.message.markup)
    assert any(cd.endswith(":party") for cd in flat if cd.startswith("reg_prompt_edit:"))


def test_reg_prompt_track_switch_is_capability_guarded():
    # Phase 8 / D-01: same migration as test_reg_q_track_switch_is_capability_guarded above.
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "reg_prompt_track_switch" in names
    assert required_capability(callback_data="reg_prompt_track:party") == "settings"


def test_reg_prompt_edit_party_suffix_sets_party_setting_key(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_prompt_edit:full_name:party")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.reg_prompt_edit(cb, state))
    data = asyncio.run(state.get_data())
    assert data["setting_key"] == "reg_prompt_full_name__party"


def test_reg_prompt_edit_no_suffix_sets_global_setting_key(tmp_path):
    """Regression: the unsuffixed global editor keeps writing reg_prompt_{step} exactly
    as before — no behavior change for the full-track editor."""
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_prompt_edit:full_name")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.reg_prompt_edit(cb, state))
    data = asyncio.run(state.get_data())
    assert data["setting_key"] == "reg_prompt_full_name"


def test_admin_reg_prompts_renders_full_track_by_default(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("admin_reg_prompts")
    asyncio.run(admin_mod.admin_reg_prompts(cb))
    assert cb.message.edit_calls == 1
    flat = _flat_callback_data(cb.message.markup)
    edit_callbacks = [cd for cd in flat if cd.startswith("reg_prompt_edit:")]
    assert edit_callbacks
    assert all(not cd.endswith(":party") for cd in edit_callbacks)


def test_rcpt_confirm_full_track_delegate_gets_none_participant_type(tmp_path, monkeypatch):
    """A full-track (or untracked) delegate's completion call passes participant_type
    resolved from the DB row, matching approve_user's pattern exactly."""
    _admin_ready(tmp_path)
    uid = 800002
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": "Full Payer", "registration_date": "2026-07-01",
        "participant_type": "full", "payment_status": "receipt_sent",
    }))

    import services.scheduler as scheduler_mod
    monkeypatch.setattr(scheduler_mod, "cancel_payment_reminders", lambda user_id: None)

    sent = {}

    async def fake_completion(bot, telegram_id, with_menu=True, participant_type=None):
        sent["participant_type"] = participant_type

    monkeypatch.setattr("handlers.registration.send_completion_and_bonus", fake_completion)

    async def fake_show_current_receipt_card(target, state):
        pass

    monkeypatch.setattr(admin_mod, "_show_current_receipt_card", fake_show_current_receipt_card)

    class FakeBot:
        async def send_message(self, *a, **k):
            pass

    class FakeCallback:
        data = f"rcpt_confirm:{uid}"
        from_user = FakeUser(ADMIN_ID)
        bot = FakeBot()
        message = FakeMessage()

        async def answer(self, text=None, show_alert=False):
            pass

    class FakeState:
        pass

    asyncio.run(admin_mod.rcpt_confirm(FakeCallback(), FakeState()))

    assert sent["participant_type"] == "full"
