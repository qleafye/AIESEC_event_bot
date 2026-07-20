"""Phase 5 Plan 3 (admin UI) tests: tri-state party toggle, track switcher, 🎉 Party
preset routing, party module/approval toggles, track line on the moderation card, and
the participant_type broadcast filter whitelist.

pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async
helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.registration import REG_FLOW, REG_PRESETS


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


def test_reg_q_track_switch_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_q_track:party", user_id=1)
    asyncio.run(admin_mod.reg_q_track_switch(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers and cb.answers[0][1] is True  # show_alert denial


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


def test_reg_q_ptoggle_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    setting_key = REG_FLOW[0][1]
    cb = FakeCallback(f"reg_q_ptoggle:{setting_key}", user_id=1)
    asyncio.run(admin_mod.toggle_party_question(cb))
    assert asyncio.run(db.get_setting(f"{setting_key}__party")) is None
    assert cb.answers and cb.answers[0][1] is True


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
    """D-07: applying the party preset must never write a bare reg_q_* global key."""
    _admin_ready(tmp_path)
    cb = FakeCallback("preset_confirm:party")
    asyncio.run(admin_mod.preset_confirm(cb))
    for _step_key, setting_key, *_rest in REG_FLOW:
        assert asyncio.run(db.get_setting(setting_key)) is None
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
