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
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
import cities
from handlers import admin as admin_mod
from handlers import admin_gamification
from handlers import registration as reg_mod
from handlers.admin_caps import CapabilityMiddleware, required_capability


ADMIN_ID = 940801
MANAGER_ID = 940802     # holds moderate_game only (game_manager)
STRANGER_ID = 940803    # holds no role, not a superadmin
MANAGER_SPB_ID = 940804  # bound to spb, NOT in config.ADMIN_IDS


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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# UAT-02: separate /start greeting for already-registered delegates
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _KBUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "testuser"
        self.full_name = "Test User"


class _KBChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    """Copied from tests/test_admin_rereg_260817.py -- captures every (text, reply_markup)
    sent via .answer()/.answer_photo()."""

    def __init__(self, text, uid):
        self.text = text
        self.from_user = _KBUser(uid)
        self.chat = _KBChat(uid)
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((caption, reply_markup))

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.text, self.from_user.id)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


def _register_approved_user(uid, status="approved"):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": "Возвращённый Делегат",
        "registration_date": "2026-08-01",
    }))
    asyncio.run(db.set_user_status(uid, status))


RETURNING_UID = 940901  # NOT in config.ADMIN_IDS


def test_returning_delegate_unset_key_gets_default_registered_text(tmp_path):
    _ready(tmp_path)
    _register_approved_user(RETURNING_UID)

    message = _KBCapturingMessage("/start", RETURNING_UID)
    state = _new_state(RETURNING_UID)
    asyncio.run(reg_mod.cmd_start(message, state, bot=object(), command=None))

    assert message.sent
    sent_text = message.sent[0][0]
    assert sent_text == reg_mod.DEFAULT_START_REGISTERED_TEXT
    assert reg_mod.DEFAULT_START_TEXT not in sent_text
    assert "5-7 минут" not in sent_text


def test_returning_delegate_gets_registered_default_even_if_start_text_customized(tmp_path):
    """This is the exact UAT-02 bug: start_text set, start_text_registered NOT set -- the
    returning delegate must still see the RETURNING default, not the customized newcomer text."""
    _ready(tmp_path)
    _register_approved_user(RETURNING_UID)
    asyncio.run(db.set_setting("start_text", "Заявка займёт 5-7 минут, заполни анкету!"))

    message = _KBCapturingMessage("/start", RETURNING_UID)
    state = _new_state(RETURNING_UID)
    asyncio.run(reg_mod.cmd_start(message, state, bot=object(), command=None))

    sent_text = message.sent[0][0]
    assert sent_text == reg_mod.DEFAULT_START_REGISTERED_TEXT
    assert "5-7 минут" not in sent_text


def test_returning_delegate_custom_registered_text_sent_verbatim(tmp_path):
    _ready(tmp_path)
    _register_approved_user(RETURNING_UID)
    asyncio.run(db.set_setting("start_text_registered", "Рады снова видеть, чемпион!"))

    message = _KBCapturingMessage("/start", RETURNING_UID)
    state = _new_state(RETURNING_UID)
    asyncio.run(reg_mod.cmd_start(message, state, bot=object(), command=None))

    assert message.sent[0][0] == "Рады снова видеть, чемпион!"


def test_returning_delegate_cleared_key_falls_back_to_default(tmp_path):
    _ready(tmp_path)
    _register_approved_user(RETURNING_UID)
    asyncio.run(db.set_setting("start_text_registered", "temp"))
    asyncio.run(db.delete_setting("start_text_registered"))

    message = _KBCapturingMessage("/start", RETURNING_UID)
    state = _new_state(RETURNING_UID)
    asyncio.run(reg_mod.cmd_start(message, state, bot=object(), command=None))

    assert message.sent[0][0] == reg_mod.DEFAULT_START_REGISTERED_TEXT


def test_newcomer_path_unaffected_gets_start_text(tmp_path):
    _ready(tmp_path)
    # no user row at all -- newcomer path
    message = _KBCapturingMessage("/start", 940999)
    state = _new_state(940999)
    asyncio.run(reg_mod.cmd_start(message, state, bot=object(), command=None))

    assert message.sent
    sent_text = message.sent[0][0]
    assert sent_text == reg_mod.DEFAULT_START_TEXT


def test_start_text_registered_key_in_registry_and_admin_screen():
    from settings_schema import SETTINGS_SCHEMA
    assert "start_text_registered" in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["start_text_registered"]["type"] == "text"
    assert SETTINGS_SCHEMA["start_text_registered"]["group"] == "event"

    assert "start_text_registered" in admin_mod._settings_group_keys("event")
    assert "start_text_registered" in admin_mod._EVENT_FIELD_ORDER
    assert "start_text_registered" in admin_mod.HTML_SETTINGS


# ═══════════════════════════════════════════════════════════════════════════════════════════
# UAT-03: city-bound game manager can only create tasks for own city
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _GTUser:
    def __init__(self, uid):
        self.id = uid


class _GTMessage:
    def __init__(self, uid=ADMIN_ID):
        self.from_user = _GTUser(uid)
        self.sent = []
        self.state_set = None

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append((text, reply_markup))


class _GTCallback:
    def __init__(self, data, uid=ADMIN_ID):
        self.data = data
        self.from_user = _GTUser(uid)
        self.message = _GTMessage(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb: InlineKeyboardMarkup) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


def _bind_manager_to_spb(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))


def test_bound_task_city_superadmin_always_none(tmp_path):
    _bind_manager_to_spb(tmp_path)
    asyncio.run(db.set_staff_city(ADMIN_ID, "spb"))  # even if a staff row + city exists
    assert asyncio.run(admin_gamification._bound_task_city(ADMIN_ID)) is None


def test_bound_task_city_unbound_manager_none(tmp_path):
    _bind_manager_to_spb(tmp_path)
    unbound_id = 940805
    asyncio.run(db.add_staff(unbound_id, "game_manager", ADMIN_ID))
    assert asyncio.run(admin_gamification._bound_task_city(unbound_id)) is None


def test_bound_task_city_bound_manager_returns_city(tmp_path):
    _bind_manager_to_spb(tmp_path)
    assert asyncio.run(admin_gamification._bound_task_city(MANAGER_SPB_ID)) == "spb"


def test_game_task_proof_done_bound_manager_skips_city_step(tmp_path):
    _bind_manager_to_spb(tmp_path)
    state = _new_state(MANAGER_SPB_ID)
    asyncio.run(state.update_data(gt_proof_types=[]))
    cb = _GTCallback("gtproof_done", MANAGER_SPB_ID)

    asyncio.run(admin_gamification.game_task_proof_done(cb, state))

    data = asyncio.run(state.get_data())
    assert data.get("gt_event_city") == "spb"
    assert data.get("gt_city_step_shown") is True
    assert data.get("gt_event_city_label") == asyncio.run(cities.city_label("spb"))
    assert asyncio.run(state.get_state()) == "GameTaskCreate:deadline"
    # "Кому задание?" screen must not have been shown
    assert not any("Кому задание" in (t or "") for t, _ in cb.message.sent)


def test_render_confirm_card_shows_kому_line_for_bound_manager(tmp_path):
    _bind_manager_to_spb(tmp_path)
    state = _new_state(MANAGER_SPB_ID)
    asyncio.run(state.update_data(gt_proof_types=[]))
    cb = _GTCallback("gtproof_done", MANAGER_SPB_ID)
    asyncio.run(admin_gamification.game_task_proof_done(cb, state))
    data = asyncio.run(state.get_data())
    data.update({"gt_text": "t", "gt_category": "Light", "gt_coins": 10, "gt_deadline": "2099-01-01 00:00:00"})
    card = admin_gamification._render_game_task_confirm_card(data)
    assert "Кому:" in card


def test_game_task_city_step_bound_manager_rejects_other_city(tmp_path):
    _bind_manager_to_spb(tmp_path)
    state = _new_state(MANAGER_SPB_ID)
    asyncio.run(state.update_data(gt_event_city="spb", gt_event_city_label="Санкт-Петербург"))
    cb = _GTCallback("gttcity:msk", MANAGER_SPB_ID)

    asyncio.run(admin_gamification.game_task_city_step(cb, state))

    assert cb.answers
    text, alert = cb.answers[-1]
    assert alert is True
    assert "только для вашего города" in text
    data = asyncio.run(state.get_data())
    assert data.get("gt_event_city") == "spb"


def test_game_task_city_step_bound_manager_rejects_all(tmp_path):
    """Bound to the DEFAULT city specifically: normalize_city('all') would otherwise collapse
    to the default city and slip through the naive 'normalize_city(code) != bound' check."""
    tmp = tmp_path
    _ready(tmp)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    default_code = cities.default_city_code()
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, default_code))

    state = _new_state(MANAGER_SPB_ID)
    asyncio.run(state.update_data(gt_event_city=default_code, gt_event_city_label="X"))
    cb = _GTCallback("gttcity:all", MANAGER_SPB_ID)

    asyncio.run(admin_gamification.game_task_city_step(cb, state))

    assert cb.answers
    text, alert = cb.answers[-1]
    assert alert is True
    assert "только для вашего города" in text


def test_game_task_city_step_bound_manager_own_city_passes(tmp_path):
    _bind_manager_to_spb(tmp_path)
    state = _new_state(MANAGER_SPB_ID)
    asyncio.run(state.update_data())
    cb = _GTCallback("gttcity:spb", MANAGER_SPB_ID)

    asyncio.run(admin_gamification.game_task_city_step(cb, state))

    assert asyncio.run(state.get_state()) == "GameTaskCreate:deadline"
    data = asyncio.run(state.get_data())
    assert data.get("gt_event_city") == "spb"


def test_game_task_city_step_superadmin_unrestricted(tmp_path):
    _bind_manager_to_spb(tmp_path)
    state = _new_state(ADMIN_ID)
    cb = _GTCallback("gttcity:msk", ADMIN_ID)

    asyncio.run(admin_gamification.game_task_city_step(cb, state))

    assert asyncio.run(state.get_state()) == "GameTaskCreate:deadline"
    data = asyncio.run(state.get_data())
    assert data.get("gt_event_city") == "msk"


def test_game_task_proof_done_module_off_byte_identical_path(tmp_path):
    """Module off entirely -- gate lives only in the module-on branch, path stays untouched."""
    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    # event_city_enabled never set -- module off
    state = _new_state(MANAGER_SPB_ID)
    asyncio.run(state.update_data(gt_proof_types=[]))
    cb = _GTCallback("gtproof_done", MANAGER_SPB_ID)

    asyncio.run(admin_gamification.game_task_proof_done(cb, state))

    data = asyncio.run(state.get_data())
    assert data.get("gt_event_city") is None
    assert data.get("gt_city_step_shown") is False
    assert asyncio.run(state.get_state()) == "GameTaskCreate:deadline"


def test_gate_no_legacy_admin_check_remains_in_bound_task_city():
    with open("handlers/admin.py", encoding="utf-8") as f:
        lines = f.readlines()
    code_lines = [ln for ln in lines if not ln.strip().startswith("#")]
    joined = "".join(code_lines)
    assert "not in config.ADMIN_IDS" not in joined
