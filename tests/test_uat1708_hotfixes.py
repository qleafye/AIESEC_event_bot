"""Quick 260817-hvy — three hotfixes из живого UAT 17.08 на тест-боте.

UAT-01: `CapabilityMiddleware` запирала менеджера в мастере навсегда, если у него отзывали
право посреди мастера (`_required_caps_for_message` требует и cap команды, и cap стейта —
не выполнить оба нечем, до рестарта бота).
UAT-02: ветка «уже зарегистрирован» в `cmd_start` слала тот же текст, что и новичку.
UAT-03: `_game_task_city_kb`/`game_task_city_step` не учитывали привязку менеджера к городу.

Харнессы скопированы (не импортированы) из tests/test_audit_criticals_260816.py (UAT-01),
tests/test_admin_rereg_260817.py (UAT-02) и tests/test_game_city_tasks_091.py +
tests/test_manager_city_091.py (UAT-03) — конвенция этого проекта.

pytest-asyncio недоступен в этом окружении — весь async управляется через asyncio.run(),
config.DB_PATH указывает на tmp_path.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers.admin_caps import CapabilityMiddleware, required_capability


ADMIN_ID = 940801
MANAGER_ID = 940802     # holds moderate_game only (game_manager)
STRANGER_ID = 940803    # holds no role, not a superadmin


def _ready(tmp_path, dbname="test_uat1708_hotfixes.db"):
    config.DB_PATH = str(tmp_path / dbname)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# UAT-01: revoked mid-wizard capability no longer locks a manager out
# ═══════════════════════════════════════════════════════════════════════════════════════════

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


class _FakeCallbackEvent:
    """Callback-shaped double: hasattr(data) and not hasattr(text) (duck-typing predicate)."""

    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_manager_locked_in_wizard_after_broadcast_revoked_admin_unlocks(tmp_path):
    """CRITICAL (UAT-01): manager held `moderate_game` + `broadcast`, was mid-wizard in
    Broadcast.target_selection, then `broadcast` got revoked. Before the fix: /admin needs
    BOTH cap:admin_menu (ANY_CAPABILITY, satisfied) AND state cap `broadcast` (missing) ->
    denied forever. After the fix: the stale wizard is closed, human-readable text mentions
    /admin, FSM state is actually cleared."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))  # moderate_game only now

    fsm = _new_state(MANAGER_ID)
    asyncio.run(fsm.set_state("Broadcast:target_selection"))

    event = _FakeMsg("/admin", MANAGER_ID)
    data = {
        "event_from_user": _FakeUser(MANAGER_ID),
        "raw_state": "Broadcast:target_selection",
        "state": fsm,
    }
    result, calls = _run_middleware(event, data)

    assert calls == [], "the /admin handler itself must not run on this message"
    assert event.answers, "human must be told what to do"
    assert "/admin" in event.answers[-1]
    assert asyncio.run(fsm.get_state()) is None, "stale wizard state must actually be cleared"


def test_manager_second_admin_after_unlock_opens_menu(tmp_path):
    """Follow-up message after the stale-wizard close: raw_state is now empty (the FSM was
    cleared), so /admin resolves via the ordinary ANY_CAPABILITY rule and the handler runs."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))

    fsm = _new_state(MANAGER_ID)
    # state already cleared by the previous message (simulated directly, not re-running the
    # first middleware call, to keep this test focused on the second message alone)

    event = _FakeMsg("/admin", MANAGER_ID)
    data = {"event_from_user": _FakeUser(MANAGER_ID), "raw_state": None, "state": fsm}
    result, calls = _run_middleware(event, data)

    assert calls == [event], "second /admin, no raw_state left, must reach the handler"


def test_locked_manager_export_still_denied_before_and_after_unlock(tmp_path):
    """The wizard-close mechanism must not open new doors: /export needs `stats`, which this
    manager never holds, on the first message AND after the stale-wizard reset."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))

    fsm = _new_state(MANAGER_ID)
    asyncio.run(fsm.set_state("Broadcast:target_selection"))

    event = _FakeMsg("/export", MANAGER_ID)
    data = {
        "event_from_user": _FakeUser(MANAGER_ID),
        "raw_state": "Broadcast:target_selection",
        "state": fsm,
    }
    result, calls = _run_middleware(event, data)
    assert calls == [], "export must not run on the first (wizard-closing) message"

    # Second message: raw_state now empty (wizard closed), /export still denied on its own cap.
    event2 = _FakeMsg("/export", MANAGER_ID)
    data2 = {"event_from_user": _FakeUser(MANAGER_ID), "raw_state": None, "state": fsm}
    result2, calls2 = _run_middleware(event2, data2)
    assert calls2 == [], "export must not run after the wizard-closing reset either"


def test_state_cap_holder_not_reset_state_stays(tmp_path):
    """The manager who legitimately still holds the wizard's capability is untouched: the
    handler runs and the FSM state is NOT cleared."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))  # holds moderate_game

    fsm = _new_state(MANAGER_ID)
    asyncio.run(fsm.set_state("GameTaskCreate:text"))

    event = _FakeMsg("моё готовое задание", MANAGER_ID)
    data = {
        "event_from_user": _FakeUser(MANAGER_ID),
        "raw_state": "GameTaskCreate:text",
        "state": fsm,
    }
    result, calls = _run_middleware(event, data)

    assert calls == [event]
    assert asyncio.run(fsm.get_state()) == "GameTaskCreate:text"


def test_foreign_state_never_reset_registration_untouched(tmp_path):
    """`Registration:*` has no state:*-key in ADMIN_CAPS -> required_capability(raw_state=...)
    is None for it -> the emergency-exit branch must never fire, even if 'state' is in data."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))
    assert required_capability(raw_state="Registration:full_name") is None

    fsm = _new_state(MANAGER_ID)
    asyncio.run(fsm.set_state("Registration:full_name"))

    event = _FakeMsg("some admin-ish text but not a command", MANAGER_ID)
    data = {
        "event_from_user": _FakeUser(MANAGER_ID),
        "raw_state": "Registration:full_name",
        "state": fsm,
    }
    _run_middleware(event, data)

    assert asyncio.run(fsm.get_state()) == "Registration:full_name"


def test_stranger_locked_in_wizard_state_reset_but_silent(tmp_path):
    """A stranger with zero capabilities gets the state cleared (DoS mitigation must apply to
    everyone) but no text is sent (D-04: admin surface not revealed to strangers), UNHANDLED
    propagates."""
    _ready(tmp_path)

    fsm = _new_state(STRANGER_ID)
    asyncio.run(fsm.set_state("Broadcast:target_selection"))

    event = _FakeMsg("/admin", STRANGER_ID)
    data = {
        "event_from_user": _FakeUser(STRANGER_ID),
        "raw_state": "Broadcast:target_selection",
        "state": fsm,
    }
    result, calls = _run_middleware(event, data)

    assert calls == []
    assert event.answers == []
    from aiogram.dispatcher.event.bases import UNHANDLED
    assert result is UNHANDLED
    assert asyncio.run(fsm.get_state()) is None


def test_callback_from_revoked_manager_still_denied_no_reset(tmp_path):
    """Callbacks are explicitly out of scope for the emergency exit -- 'Недостаточно прав'
    toast as before, no FSM touched (the callback path never sets raw_state)."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))

    fsm = _new_state(MANAGER_ID)
    asyncio.run(fsm.set_state("Broadcast:target_selection"))

    event = _FakeCallbackEvent("broadcast_cancel", MANAGER_ID)
    data = {"event_from_user": _FakeUser(MANAGER_ID), "raw_state": "Broadcast:target_selection",
            "state": fsm}
    result, calls = _run_middleware(event, data)

    assert calls == []
    assert event.answers == [("Недостаточно прав", True)]
    assert asyncio.run(fsm.get_state()) == "Broadcast:target_selection"


def test_no_state_in_data_behaves_as_before(tmp_path):
    """Unit-style middleware calls without data['state'] (e.g. many existing tests) must not
    change behavior: _close_stale_wizard returns False, falls through to ordinary _deny."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))

    event = _FakeMsg("/admin", MANAGER_ID)
    data = {"event_from_user": _FakeUser(MANAGER_ID), "raw_state": "Broadcast:target_selection"}
    result, calls = _run_middleware(event, data)

    assert calls == []
    assert event.answers == ["Недостаточно прав."]
