"""Phase 8 (roles-and-capability-based-access) — Wave-0 test file + dispatch harness.

Covers ROLE-01/ROLE-02 model tests (`resolve_capabilities`, `staff` accessors) plus the
dispatch harness itself, which drives a REAL `Router.propagate_event` (not a direct handler
call) — every existing admin test bypasses aiogram dispatch entirely, so this is the only
place in the suite that can prove middleware (added in a later phase-8 plan) actually
intercepts an event. Reused verbatim by 08-03/08-05 (see 08-01-PLAN.md interfaces).

pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async
helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.

The capability-model tests below (`-k capabilities`/`-k bootstrap_compat`) import
`handlers.admin_caps` LAZILY, inside the test body — that module does not exist until Task 2
of this plan, and a top-level import would break collection for the WHOLE file. Their
RED failure (AttributeError/ImportError) at this stage is expected and intentional.
"""
import asyncio

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod


ADMIN_ID = 900801
MANAGER_ID = 900802
GAME_MANAGER_ID = 900803
STRANGER_ID = 900804


def _roles_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_roles_phase8.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    """Dual-purpose fake: works as `callback.message` (edit_text) AND as a dispatched
    message-event itself (text/chat/reply_to_message/html_text + answer/reply)."""

    def __init__(self, text=None, user_id=None, chat_id=None, reply_to_message=None):
        self.text = text
        self.html_text = text
        self.markup = None
        self.edit_calls = 0
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(chat_id if chat_id is not None else user_id)
        self.reply_to_message = reply_to_message
        self.answers = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))

    async def reply(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def dispatch_callback(data, user_id, *, bot=None, state=None):
    """Drive a real CallbackQuery through `admin_mod.router.propagate_event` — bypasses no
    middleware, unlike every existing admin test (which calls handler functions directly).
    Returns (result, event); compare `result` against `aiogram.dispatcher.event.bases.UNHANDLED`.
    """
    if bot is None:
        bot = FakeBot()
    if state is None:
        state = _fresh_state(user_id)
    event = FakeCallback(data, user_id=user_id)
    kwargs = dict(
        event_from_user=FakeUser(user_id),
        bot=bot,
        raw_state=None,
        state=state,
        event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("callback_query", event, **kwargs))
    return result, event


def dispatch_message(text, user_id, *, raw_state=None, reply_to=None, bot=None, state=None):
    """Same as `dispatch_callback` but for the "message" observer — needed for `/command`
    handlers and FSM-continuation (StateFilter-gated) admin wizard steps."""
    if bot is None:
        bot = FakeBot()
    if state is None:
        state = _fresh_state(user_id)
    event = FakeMessage(text=text, user_id=user_id, chat_id=user_id, reply_to_message=reply_to)
    kwargs = dict(
        event_from_user=FakeUser(user_id),
        bot=bot,
        raw_state=raw_state,
        state=state,
        event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("message", event, **kwargs))
    return result, event


# ── Harness proof (Pitfall 4): the harness genuinely goes through Router.trigger/middleware,
# not a direct function call. ──────────────────────────────────────────────────────────────

def test_harness_dispatches_a_real_callback_through_the_router(tmp_path):
    _roles_ready(tmp_path)
    result, event = dispatch_callback("admin_stats", ADMIN_ID)
    assert result is not UNHANDLED
    assert event.message.edit_calls == 1


def test_harness_returns_unhandled_for_foreign_callback(tmp_path):
    # "pay_option:0" belongs to payment.router, never registered on admin_mod.router —
    # confirms the admin router does not claim events it has no handler for.
    _roles_ready(tmp_path)
    result, event = dispatch_callback("pay_option:0", ADMIN_ID)
    assert result is UNHANDLED


# ── Capability model (RED until Task 2 — handlers.admin_caps does not exist yet) ───────────

def test_capabilities_of_staff_member_are_role_caps(tmp_path):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    caps = asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID))
    assert caps == {"moderate_reg", "moderate_receipts"}


def test_capabilities_union_across_two_roles(tmp_path):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))
    caps = asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID))
    assert caps == {"moderate_reg", "moderate_receipts", "moderate_game"}


def test_disabled_role_grants_nothing(tmp_path):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_setting("role_reg_manager_enabled", "off"))
    caps = asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID))
    assert caps == set()


def test_bootstrap_admin_has_all_capabilities_with_empty_staff(tmp_path):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    caps = asyncio.run(admin_caps.resolve_capabilities(ADMIN_ID))
    assert caps == set(admin_caps.ALL_CAPABILITIES)


def test_stranger_has_no_capabilities(tmp_path):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    caps = asyncio.run(admin_caps.resolve_capabilities(STRANGER_ID))
    assert caps == set()
