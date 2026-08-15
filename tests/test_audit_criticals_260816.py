"""Regression tests for the two CRITICAL findings fixed on main (night audit 260815).

1. CapabilityMiddleware escalation: a slash-command typed while an FSM wizard is active was
   waved through on the WIZARD state's capability, while aiogram's first-match dispatch runs
   the top-level COMMAND handler — letting e.g. a game_manager (moderate_game only) run
   /export (needs stats). Fix: a slash-command message must clear the command's OWN capability
   as well as the state's (handlers/admin_caps.py::_required_caps_for_message).

2. settings_edit_value stored the empty string for a non-text / whitespace-only send
   (message.text is None -> value ""), which the registry's text branch returns instead of the
   default and which breaks the allowlist read / Google Sheets sync when it lands on a tab-name
   key. Fix: empty value is rejected with guidance, nothing is saved (handlers/admin.py).

pytest-asyncio is unavailable here — async is driven via asyncio.run(), config.DB_PATH -> tmp.
Fakes are copied (not imported) per this suite's convention.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import CapabilityMiddleware, required_capability


ADMIN_ID = 900801
GAME_MANAGER_ID = 900803


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_audit_criticals.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


# ── Fix 1: CapabilityMiddleware — slash-command-in-wizard escalation ─────────────────────────

class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = None


class _FakeMsg:
    def __init__(self, text, uid, reply_to_message=None):
        self.text = text
        self.html_text = text
        self.from_user = _FakeUser(uid)
        self.reply_to_message = reply_to_message
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


def _run_middleware(event, data):
    calls = []

    async def _spy(ev, d):
        calls.append(ev)
        return "SPY_OK"

    result = asyncio.run(CapabilityMiddleware()(_spy, event, data))
    return result, calls


def test_slash_command_in_wizard_requires_command_capability(tmp_path):
    """CRITICAL: game_manager (moderate_game only) inside GameTaskCreate types /export.
    /export needs `stats`; must be DENIED even though the wizard state's cap is held."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    # sanity: the two candidate caps genuinely differ
    assert required_capability(command="export") == "stats"
    assert required_capability(raw_state="GameTaskCreate:text") == "moderate_game"

    event = _FakeMsg("/export", GAME_MANAGER_ID)
    data = {"event_from_user": _FakeUser(GAME_MANAGER_ID), "raw_state": "GameTaskCreate:text"}
    result, calls = _run_middleware(event, data)

    assert calls == [], "escalation: privileged command executed on the wizard's capability"
    assert event.answers, "a known manager denied a capability should get the toast"


def test_slash_command_coins_in_editsetting_denied_for_settings_holder(tmp_path):
    """CRITICAL sibling: /coins (needs moderate_reg) typed inside EditSetting.waiting_for_value.
    A settings-only holder must not mint coins via the state's `settings` cap."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))  # holds moderate_game only
    event = _FakeMsg("/coins 123 500", GAME_MANAGER_ID)
    data = {"event_from_user": _FakeUser(GAME_MANAGER_ID), "raw_state": "EditSetting:waiting_for_value"}
    result, calls = _run_middleware(event, data)
    assert calls == []


def test_plain_wizard_text_still_allowed_on_state_cap(tmp_path):
    """Regression guard: ordinary (non-command) wizard text is gated by the STATE only —
    behavior unchanged by the fix."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    event = _FakeMsg("моё готовое задание", GAME_MANAGER_ID)
    data = {"event_from_user": _FakeUser(GAME_MANAGER_ID), "raw_state": "GameTaskCreate:text"}
    result, calls = _run_middleware(event, data)
    assert calls == [event]


def test_unmapped_slash_command_in_wizard_uses_state_cap(tmp_path):
    """Regression guard: /cancel is deliberately unmapped as a command (cmd:cancel absent) —
    both real /cancel handlers are StateFilter-gated. It must still resolve via the state cap,
    not be denied for the unmapped command."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    assert required_capability(command="cancel") is None
    event = _FakeMsg("/cancel", GAME_MANAGER_ID)
    data = {"event_from_user": _FakeUser(GAME_MANAGER_ID), "raw_state": "GameTaskCreate:text"}
    result, calls = _run_middleware(event, data)
    assert calls == [event]


def test_admin_passes_slash_command_in_wizard(tmp_path):
    """Positive control: a full admin (every capability) is not over-denied by the AND rule."""
    _ready(tmp_path)
    event = _FakeMsg("/export", ADMIN_ID)
    data = {"event_from_user": _FakeUser(ADMIN_ID), "raw_state": "GameTaskCreate:text"}
    result, calls = _run_middleware(event, data)
    assert calls == [event]


# ── Fix 2: settings_edit_value — empty / non-text value rejected ─────────────────────────────

class _FakeSettingsMessage:
    def __init__(self, uid=ADMIN_ID, text=""):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.html_text = text
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


class _FakeFSMState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self._state = "EditSetting:waiting_for_value"

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


def test_non_text_message_does_not_store_empty_tab_name(tmp_path):
    """CRITICAL: a sticker/photo (message.text is None) while editing main_sheet_tab must NOT
    store "" — that empty tab name breaks the allowlist read and every sheet sync."""
    _ready(tmp_path)
    message = _FakeSettingsMessage(text=None)
    state = _FakeFSMState({"setting_key": "main_sheet_tab"})

    async def go():
        await admin_mod.settings_edit_value(message, state)
        return await db.get_setting("main_sheet_tab")

    saved = asyncio.run(go())
    assert saved is None, "empty value was stored for a tab-name key"
    assert message.answers, "admin should get guidance to resend as text"
    assert "текст" in message.answers[-1].lower()
    # state not cleared — admin stays in the edit flow to retype
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"


def test_whitespace_only_value_rejected(tmp_path):
    _ready(tmp_path)
    message = _FakeSettingsMessage(text="   ")
    state = _FakeFSMState({"setting_key": "event_place_name"})

    async def go():
        await admin_mod.settings_edit_value(message, state)
        return await db.get_setting("event_place_name")

    saved = asyncio.run(go())
    assert saved is None
    assert message.answers


def test_normal_text_value_still_saves(tmp_path):
    """Positive control: a real text value is unaffected by the empty-value guard."""
    _ready(tmp_path)
    message = _FakeSettingsMessage(text="Площадка Заря")
    state = _FakeFSMState({"setting_key": "event_place_name"})

    async def go():
        await admin_mod.settings_edit_value(message, state)
        return await db.get_setting("event_place_name")

    saved = asyncio.run(go())
    assert saved == "Площадка Заря"
    assert asyncio.run(state.get_state()) is None  # cleared on success
