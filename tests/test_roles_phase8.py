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
import inspect
import re
from pathlib import Path

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import states as states_mod


ADMIN_ID = 900801
MANAGER_ID = 900802
GAME_MANAGER_ID = 900803
STRANGER_ID = 900804


def _roles_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_roles_phase8.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


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


# ── Task 1 (D-18): «Роли и доступы» screen — entry row, render, role toggles ───────────────

def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_roles_group_ui_row_is_in_settings_not_in_admin_menu(tmp_path):
    _roles_ready(tmp_path)
    settings_kb = asyncio.run(admin_mod.build_settings_keyboard())
    assert "admin_roles" in _flat_callback_data(settings_kb)

    # 08-05 (D-15): build_admin_keyboard is now async + capability-filtered.
    admin_kb = asyncio.run(admin_mod.build_admin_keyboard(ADMIN_ID))
    assert "admin_roles" not in _flat_callback_data(admin_kb)


def test_roles_screen_lists_staff_with_roles(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import ROLES

    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    text = asyncio.run(admin_mod.render_roles_text())

    assert str(MANAGER_ID) in text
    assert str(GAME_MANAGER_ID) in text
    assert ROLES["reg_manager"]["label"] in text
    assert ROLES["game_manager"]["label"] in text


def test_roles_screen_shows_role_toggle_state(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import ROLES

    asyncio.run(db.set_setting("role_game_manager_enabled", "off"))
    text = asyncio.run(admin_mod.render_roles_text())
    assert f"{ROLES['game_manager']['label']}: <b>❌ Выкл</b>" in text


def test_roles_toggle_flips_setting(tmp_path):
    _roles_ready(tmp_path)
    from settings_schema import get_setting_typed

    dispatch_callback("roles_toggle:game_manager", ADMIN_ID)
    assert asyncio.run(get_setting_typed("role_game_manager_enabled")) == "off"

    dispatch_callback("roles_toggle:game_manager", ADMIN_ID)
    assert asyncio.run(get_setting_typed("role_game_manager_enabled")) == "on"


def test_roles_caps_edit_button_opens_the_checkbox_screen(tmp_path):
    """Было: кнопка вела в generic settings_edit, который просил НАБРАТЬ коды прав текстом.
    Quick 260813 заменил это экраном с чекбоксами (tests/test_role_caps_buttons_260813.py) —
    здесь проверяется только то, что старый текстовый путь с экрана ролей больше не доступен."""
    _roles_ready(tmp_path)
    cds = _flat_callback_data(asyncio.run(admin_mod.build_roles_keyboard()))
    assert "roles_caps:reg_manager" in cds
    assert "settings_edit:role_caps_reg_manager" not in cds


# ── Task 2 (D-11/D-12/D-18): add/remove a manager — forward / @username / numeric id ───────

def test_staff_crud_add_by_numeric_id(tmp_path):
    _roles_ready(tmp_path)
    result, event = dispatch_message(str(MANAGER_ID), ADMIN_ID, raw_state="StaffAdd:waiting_for_person")
    assert result is not UNHANDLED
    markup = event.answers[-1][2]
    cds = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"roles_addrole:{MANAGER_ID}:reg_manager" in cds
    assert f"roles_addrole:{MANAGER_ID}:game_manager" in cds


def test_staff_crud_add_by_username(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": MANAGER_ID, "username": "@ivan", "full_name": "Ivan Test",
        "registration_date": "2026-01-01",
    }))
    result, event = dispatch_message("@ivan", ADMIN_ID, raw_state="StaffAdd:waiting_for_person")
    assert result is not UNHANDLED
    markup = event.answers[-1][2]
    cds = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"roles_addrole:{MANAGER_ID}:reg_manager" in cds


def test_staff_crud_add_by_forward(tmp_path):
    _roles_ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    event = FakeMessage(text=None, user_id=ADMIN_ID, chat_id=ADMIN_ID)
    event.forward_from = FakeUser(MANAGER_ID)  # no `users` row needed for forward resolution
    bot = FakeBot()
    kwargs = dict(
        event_from_user=FakeUser(ADMIN_ID), bot=bot,
        raw_state="StaffAdd:waiting_for_person", state=state, event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("message", event, **kwargs))
    assert result is not UNHANDLED
    markup = event.answers[-1][2]
    cds = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"roles_addrole:{MANAGER_ID}:reg_manager" in cds


def test_staff_crud_add_unresolvable_input_is_rejected(tmp_path):
    _roles_ready(tmp_path)
    dispatch_message("просто текст", ADMIN_ID, raw_state="StaffAdd:waiting_for_person")
    assert asyncio.run(db.list_staff()) == []


def test_staff_crud_assign_and_remove(tmp_path):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    dispatch_callback(f"roles_addrole:{MANAGER_ID}:reg_manager", ADMIN_ID)
    assert asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID)) == {"moderate_reg", "moderate_receipts"}

    dispatch_callback(f"roles_del:{MANAGER_ID}:reg_manager", ADMIN_ID)
    assert asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID)) == set()


def test_staff_crud_bootstrap_admin_cannot_be_removed(tmp_path):
    _roles_ready(tmp_path)
    result, event = dispatch_callback(f"roles_del:{ADMIN_ID}:reg_manager", ADMIN_ID)
    assert any("суперадмин" in (text or "").lower() for text, _show_alert in event.answers)
    assert asyncio.run(db.get_staff_roles(ADMIN_ID)) == []  # nothing to remove; ADMIN_IDS untouched


def test_staff_crud_duplicate_add_is_idempotent(tmp_path):
    _roles_ready(tmp_path)
    dispatch_callback(f"roles_addrole:{MANAGER_ID}:reg_manager", ADMIN_ID)
    dispatch_callback(f"roles_addrole:{MANAGER_ID}:reg_manager", ADMIN_ID)
    staff = asyncio.run(db.list_staff())
    matches = [s for s in staff if s["telegram_id"] == MANAGER_ID and s["role"] == "reg_manager"]
    assert len(matches) == 1


# ── Task 1 (D-01/D-02/D-03): completeness of ADMIN_CAPS + required_capability() ────────────
#
# The mechanism (08-03-PLAN.md Task 1 <action>, RESEARCH Pitfall 2): a `MagicFilter`'s literal
# argument (`F.data == "x"` / `.startswith("x")`) has no public accessor -- so instead of
# tagging every handler with `@flags.cap(...)`, we derive the expected capability KEY from the
# ACTUAL decorator source text (`inspect.getsource`) and feed it through the real
# `required_capability()` resolver. This is strictly stronger than a hand-maintained parallel
# list: it breaks the moment a decorator's literal and ADMIN_CAPS's key genuinely disagree.

# Handlers with no F.data/Command/StateFilter literal to derive a key from -- both audited by
# hand against handlers/admin.py (08-03-PLAN.md Task 1 <action>, verbatim list):
#   - admin_reply_to_question: bare predicate filter (`is_question_reply`), no literal at all
#   - filter_pick_field: `F.data.in_({f"filter_f_{fld}" for fld in _PICKER_FIELDS})` -- a set
#     COMPREHENSION over an external module-level constant, not string literals
_UNKEYED_HANDLERS = {
    "admin_reply_to_question": "special:question_reply",
    "filter_pick_field": "filter_f_*",
}


def _decorator_lines(func):
    """Every `@router.` line directly above `func` in its own source (inspect.getsource
    returns decorators + signature + body for a plain registered function)."""
    return [line for line in inspect.getsource(func).splitlines() if line.startswith("@router.")]


_STATE_GROUP_ATTR_RE = re.compile(r"\b([A-Z]\w+)\.(\w+)\b")


def _callback_keys_from_line(line):
    """`F.data == "X"` -> "X"; `.startswith("X")` -> "X*"; `.in_({"A", "B"})` (string literals,
    NOT a comprehension -- see filter_pick_field above) -> "A", "B"."""
    keys = []
    for m in re.finditer(r'F\.data\s*==\s*"([^"]+)"', line):
        keys.append(m.group(1))
    for m in re.finditer(r'F\.data\.startswith\("([^"]+)"\)', line):
        keys.append(m.group(1) + "*")
    m = re.search(r"F\.data\.in_\(\{([^}]*)\}\)", line)
    if m and " for " not in m.group(1):
        keys.extend(re.findall(r'"([^"]+)"', m.group(1)))
    return keys


def _message_keys_from_line(line):
    """State reference wins over a command literal on the SAME line (mirrors
    required_capability()'s own resolution order + the /cancel-mid-wizard case) -- checked
    first as `StateFilter(Group)`, then as a bare `Group.state_name` positional arg."""
    m = re.search(r"StateFilter\((\w+)\)", line)
    if m and hasattr(states_mod, m.group(1)):
        return [f"state:{m.group(1)}:*"]
    for group_name, _state_name in _STATE_GROUP_ATTR_RE.findall(line):
        if hasattr(states_mod, group_name):
            return [f"state:{group_name}:*"]
    m = re.search(r'Command\("([^"]+)"\)', line)
    if m:
        return [f"cmd:{m.group(1)}"]
    return []


def _keys_from_decorator(line, observer_name):
    if observer_name == "callback_query":
        return _callback_keys_from_line(line)
    return _message_keys_from_line(line)


def _keys_for_handler(handler_obj, observer_name):
    keys = []
    for line in _decorator_lines(handler_obj.callback):
        keys.extend(_keys_from_decorator(line, observer_name))
    return keys


def _resolve_key(key):
    """Feed a derived key back through the REAL required_capability() -- not a second lookup
    table -- so this test proves ADMIN_CAPS itself is complete, not just internally consistent
    with a copy of itself."""
    from handlers.admin_caps import required_capability

    if key.startswith("cmd:"):
        return required_capability(command=key[len("cmd:"):])
    if key.startswith("state:"):
        group = key.split(":")[1]
        return required_capability(raw_state=f"{group}:probe")
    if key.startswith("special:"):
        return required_capability(special=key[len("special:"):])
    if key.endswith("*"):
        return required_capability(callback_data=key[:-1] + "x")
    return required_capability(callback_data=key)


def _iter_admin_handlers():
    for observer_name in ("message", "callback_query"):
        observer = getattr(admin_mod.router, observer_name)
        for handler_obj in observer.handlers:
            yield observer_name, handler_obj


def test_completeness_every_admin_handler_resolves_to_a_capability(tmp_path):
    _roles_ready(tmp_path)
    missing = []
    for observer_name, handler_obj in _iter_admin_handlers():
        name = handler_obj.callback.__name__
        keys = _keys_for_handler(handler_obj, observer_name)
        if not keys:
            assert name in _UNKEYED_HANDLERS, (
                f"{observer_name}:{name} has no derivable key from its decorator(s) and is "
                "not in _UNKEYED_HANDLERS -- either add a matching F.data/Command/StateFilter "
                "literal or register it explicitly."
            )
            keys = [_UNKEYED_HANDLERS[name]]
        for key in keys:
            if _resolve_key(key) is None:
                missing.append((observer_name, name, key))
    assert not missing, f"Handlers whose derived key(s) resolve to no capability: {missing}"


def test_completeness_unkeyed_handlers_list_is_exactly_expected(tmp_path):
    actual_unkeyed = set()
    for observer_name, handler_obj in _iter_admin_handlers():
        if not _keys_for_handler(handler_obj, observer_name):
            actual_unkeyed.add(handler_obj.callback.__name__)
    assert actual_unkeyed == set(_UNKEYED_HANDLERS)


def test_completeness_admin_router_decorators_are_single_line():
    src_path = Path(admin_mod.__file__)
    bad = []
    for lineno, line in enumerate(src_path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("@router."):
            if line.count("(") != line.count(")") or not line.rstrip().endswith(")"):
                bad.append((lineno, line))
    assert not bad, (
        "Многострочный декоратор @router.* обнаружен — тест полноты карты прав "
        f"(tests/test_roles_phase8.py) собирает ключи построчно и перестанет их видеть: {bad}"
    )


def test_required_capability_prefix_match_is_longest_first(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import required_capability

    assert required_capability(callback_data="settings_edit:foo") == "settings"
    assert required_capability(callback_data="appr_approve:123") == "moderate_reg"
    assert required_capability(callback_data="zzz_unknown") is None


def test_required_capability_message_order_state_before_command(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import required_capability

    # /cancel mid-Broadcast-wizard resolves via the state (broadcast), not the absent
    # "cmd:cancel" -- 08-03-PLAN.md <capability_map> footnote: cmd:cancel is deliberately
    # absent from ADMIN_CAPS because both real /cancel handlers are StateFilter-gated.
    assert required_capability(command="cancel", raw_state="Broadcast:message") == "broadcast"
    assert required_capability(command="cancel") is None


def test_every_capability_value_is_known(tmp_path):
    from handlers.admin_caps import ADMIN_CAPS, ALL_CAPABILITIES, ANY_CAPABILITY

    bad = [k for k, v in ADMIN_CAPS.items() if v != ANY_CAPABILITY and v not in ALL_CAPABILITIES]
    assert not bad, bad


# ── Task 2 (D-01/D-04/D-13/T-08-15/T-08-16): CapabilityMiddleware ──────────────────────────
#
# Positive paths run through `_run_middleware` (a spy in place of the real handler), NOT
# `dispatch_callback` -- the inline `config.ADMIN_IDS` checks inside every admin.py handler
# body are STILL LIVE until 08-04 (D-03: migration happens in one atomic move, not gradually).
# A real `show_applications` call would deny a non-admin manager from ITS OWN inline check,
# which would make a "middleware lets the manager through" test pass for the wrong reason.
# The spy isolates exactly one claim: middleware called the wrapped handler.
# Negative paths use the real `dispatch_callback` -- denial happens INSIDE the middleware,
# strictly before the handler (and its inline check) ever run, so the old mechanism cannot
# interfere with a negative assertion either way.

def _run_middleware(event, data):
    from handlers.admin_caps import CapabilityMiddleware

    calls = []

    async def _spy(ev, d):
        calls.append((ev, d))
        return "SPY_OK"

    middleware = CapabilityMiddleware()
    result = asyncio.run(middleware(_spy, event, data))
    return result, calls


def test_middleware_allows_capability_holder(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    event = FakeCallback("admin_applications", user_id=MANAGER_ID)
    data = {"event_from_user": FakeUser(MANAGER_ID)}

    result, calls = _run_middleware(event, data)

    assert result == "SPY_OK"
    assert len(calls) == 1
    assert event.answers == []


def test_middleware_denies_missing_capability_with_alert(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))

    result, event = dispatch_callback("admin_applications", GAME_MANAGER_ID)

    assert event.answers == [("Недостаточно прав", True)]


def test_deny_response_is_silent_for_stranger(tmp_path):
    _roles_ready(tmp_path)

    result, event = dispatch_callback("admin_applications", STRANGER_ID)

    assert event.answers == []


def test_middleware_denies_unmapped_callback(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import required_capability

    assert required_capability(callback_data="zzz_totally_unknown") is None

    event = FakeCallback("zzz_totally_unknown", user_id=ADMIN_ID)
    data = {"event_from_user": FakeUser(ADMIN_ID)}
    result, calls = _run_middleware(event, data)

    assert calls == []


def test_stranger_event_still_propagates(tmp_path):
    _roles_ready(tmp_path)

    result, event = dispatch_callback("admin_applications", STRANGER_ID)

    assert result is UNHANDLED


def test_bootstrap_compat_admin_with_empty_staff_passes_every_key(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_caps import ADMIN_CAPS

    data = {"event_from_user": FakeUser(ADMIN_ID)}
    for key in ADMIN_CAPS:
        probe_data = key[:-1] + "x" if key.endswith("*") else key
        event = FakeCallback(probe_data, user_id=ADMIN_ID)
        result, calls = _run_middleware(event, data)
        assert calls, f"bootstrap admin denied for ADMIN_CAPS key {key!r}"


def test_middleware_does_not_touch_foreign_router_events(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    from handlers import admin_caps

    calls = []
    original = admin_caps.resolve_capabilities

    async def _counting(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(admin_caps, "resolve_capabilities", _counting)

    result, event = dispatch_callback("pay_option:0", ADMIN_ID)

    assert result is UNHANDLED
    assert calls == []


# ── 08-04 (D-01/D-03): end-to-end proof that removing the old inline ADMIN_IDS mechanism
# actually lets a non-admin manager reach the handler BODY, not just clear the middleware ──
#
# 08-03-SUMMARY.md's closing note to this plan: until the inline `config.ADMIN_IDS` checks
# were removed from every handler body, a moderate_reg-only manager who cleared
# CapabilityMiddleware on `admin_applications` still hit `show_applications`'s OWN inline
# check and got denied from a DIFFERENT code path, with a byte-identical error message --
# which would make a same-shaped end-to-end test pass for the wrong reason. This test only
# means anything AFTER 08-04 Task 1 deleted those inline checks.

def test_manager_reaches_handler_body_end_to_end(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    assert MANAGER_ID not in config.ADMIN_IDS  # genuinely NOT a bootstrap superadmin

    result, event = dispatch_callback("admin_applications", MANAGER_ID)

    # Middleware let the event through to the real router dispatch.
    assert result is not UNHANDLED
    # The handler body itself ran: show_applications -> _show_current_card(callback.message,
    # state), which always ends in target.answer(...) (empty-queue branch or a rendered
    # card) -- checked as "a call happened", not the exact card text, so this test doesn't
    # break on copy edits.
    assert event.message.answers, "handler body never called .answer on callback.message"
    # Negative half of the same claim: no surviving inline check silently denied the
    # manager from inside the handler body with the same "Недостаточно прав" alert.
    denial_texts = [text for text, _show_alert in event.answers]
    assert "Недостаточно прав" not in denial_texts


# ── 08-04 (D-17): slash commands are protected by the SAME map as callback buttons ─────────

def test_admin_commands_match_d17_mapping():
    from handlers.admin_caps import required_capability

    d17 = {
        "find": "moderate_reg",
        "coins": "moderate_reg",
        "stats": "stats",
        "stats_monthly": "stats",
        "export": "stats",
        "broadcast": "broadcast",
        "scheduled": "broadcast",
        "settings_guide": "settings",
        "refresh_allowlist": "settings",
    }
    bad = [(cmd, required_capability(command=cmd), expected)
           for cmd, expected in d17.items()
           if required_capability(command=cmd) != expected]
    assert not bad, bad


# ── 08-05 Task 1 (D-15): per-person admin menu — built from the SAME map middleware enforces ──

def test_menu_admin_sees_all_rows(tmp_path):
    _roles_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_admin_keyboard(ADMIN_ID))
    flat = _flat_callback_data(kb)
    assert flat == [cb for _, cb in admin_mod._ADMIN_MENU_ROWS]


def test_menu_reg_manager_sees_only_own_rows(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    kb = asyncio.run(admin_mod.build_admin_keyboard(MANAGER_ID))
    flat = _flat_callback_data(kb)
    assert "admin_applications" in flat
    assert "admin_receipts" in flat
    for forbidden in ("admin_broadcast", "admin_settings", "admin_stats"):
        assert forbidden not in flat


def test_menu_stranger_gets_empty_row_set(tmp_path):
    _roles_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_admin_keyboard(STRANGER_ID))
    assert kb.inline_keyboard == []


def test_menu_rows_and_map_stay_in_sync():
    from handlers.admin_caps import required_capability

    # A new menu row with no ADMIN_CAPS entry would resolve to None here and silently vanish
    # from every keyboard forever (deny-by-default, D-02) -- this test roars instead.
    bad = [cb for _, cb in admin_mod._ADMIN_MENU_ROWS if required_capability(callback_data=cb) is None]
    assert not bad, bad


def test_menu_has_no_second_map():
    # D-01/D-15's "one map" invariant: handlers/admin.py must not hand-roll a parallel
    # "button -> capability" dict anywhere -- the capability is always resolved live via
    # required_capability(), never a hardcoded literal pair copied out of ADMIN_CAPS.
    source = inspect.getsource(admin_mod)
    assert '"admin_stats": "stats"' not in source
    assert '"admin_applications": "moderate_reg"' not in source
    assert '"admin_broadcast": "broadcast"' not in source


# ── 08-05 Task 2 (D-16): /admin auto-opens the one available SCREEN, never an action ────────

def test_pick_auto_open_returns_none_for_action_only_rows():
    # T-08-25: action rows are never in _AUTO_OPEN_SECTIONS, so a single visible action row
    # (however it got there) can never trigger auto-open.
    for cb in ("admin_export_csv", "admin_export_incomplete", "admin_sync_sheet",
               "admin_rebuild_sheet", "admin_dedupe_sheet"):
        assert admin_mod._pick_auto_open([("x", cb)]) is None


def test_pick_auto_open_returns_none_for_zero_or_multiple_rows():
    assert admin_mod._pick_auto_open([]) is None
    assert admin_mod._pick_auto_open([("a", "admin_applications"), ("b", "admin_receipts")]) is None


def test_pick_auto_open_returns_the_handler_for_a_single_screen_row():
    handler, needs_state = admin_mod._pick_auto_open([("x", "admin_applications")])
    assert handler is admin_mod.show_applications
    assert needs_state is True


# NOTE: `cmd_admin_help` is Command()-filtered, and aiogram's `Command.__call__` hard-requires
# `isinstance(message, Message)` (verified: `aiogram/filters/command.py`) -- our duck-typed
# `FakeMessage` double is never an instance of the real class, so `dispatch_message`'s router
# harness can never route "/admin" to this handler (Command filter returns False before the
# router even reaches CapabilityMiddleware). The tests below call `cmd_admin_help` directly --
# the SAME established pattern 08-04-SUMMARY.md documents for every "handler BODY behavior,
# post-authorization" test in this suite (authorization itself is covered separately, by
# `test_admin_commands_match_d17_mapping` above and the `cmd:admin` -> ANY_CAPABILITY middleware
# tests elsewhere in this file).

def test_single_section_autoopens(tmp_path):
    from handlers.admin_caps import role_caps_key

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    # Isolate this person to exactly ONE menu row (admin_receipts) — reg_manager's default
    # caps (moderate_reg + moderate_receipts) would otherwise show two rows, and moderate_reg
    # alone now ALSO maps to two rows (admin_applications + T-08-33's admin_stuck_questions) --
    # moderate_receipts is the one capability that still maps to exactly one row. Registry
    # `list` values are stored as a raw newline/`;`-separated STRING
    # (settings_schema._parse_setting), not a Python list -- one capability per line here is
    # exactly one line.
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_receipts"))

    message = FakeMessage(text="/admin", user_id=MANAGER_ID, chat_id=MANAGER_ID)
    state = _fresh_state(MANAGER_ID)
    asyncio.run(admin_mod.cmd_admin_help(message, state))

    assert message.answers, "expected the auto-opened applications screen to answer"
    texts = [t for t, _pm, _rm in message.answers]
    assert not any("Панель администратора" in (t or "") for t in texts)


def test_multi_section_shows_menu(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))  # default: 2 caps -> 2 rows

    message = FakeMessage(text="/admin", user_id=MANAGER_ID, chat_id=MANAGER_ID)
    state = _fresh_state(MANAGER_ID)
    asyncio.run(admin_mod.cmd_admin_help(message, state))

    texts = [t for t, _pm, _rm in message.answers]
    assert any("Панель администратора" in (t or "") for t in texts)


def test_action_only_row_never_autoopens_end_to_end(tmp_path):
    from handlers.admin_caps import role_caps_key

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    # "settings" maps to 6 menu rows in ADMIN_CAPS (mixed screens + actions) -- there is no
    # single real capability that isolates to exactly one ACTION row, so this proves the
    # invariant through the pure decision (_pick_auto_open) already covered above, and here
    # proves the end-to-end fallback: with MULTIPLE settings-capability rows visible (several
    # of them actions), /admin never silently runs one of them, it shows the ordinary menu.
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "settings"))

    message = FakeMessage(text="/admin", user_id=MANAGER_ID, chat_id=MANAGER_ID)
    state = _fresh_state(MANAGER_ID)
    asyncio.run(admin_mod.cmd_admin_help(message, state))

    texts = [t for t, _pm, _rm in message.answers]
    assert any("Панель администратора" in (t or "") for t in texts)
    flat_markups = [rm for _t, _pm, rm in message.answers if rm is not None]
    assert flat_markups, "expected the ordinary menu keyboard to be sent"
    flat_cb = [b.callback_data for row in flat_markups[0].inline_keyboard for b in row]
    assert "admin_rebuild_sheet" in flat_cb  # action row IS shown, just never auto-run


def test_no_sections_shows_explanatory_message(tmp_path):
    _roles_ready(tmp_path)
    # 09-02 (GAME-01) gave moderate_game a menu row ("📋 Задания"), so game_manager's default
    # caps no longer map to zero rows -- override role_caps_game_manager to "checkin" (Phase 12,
    # not built yet -- still zero _ADMIN_MENU_ROWS today) to keep exercising a real,
    # currently-enabled role with nothing to show.
    from handlers.admin_caps import role_caps_key

    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_setting(role_caps_key("game_manager"), "checkin"))

    message = FakeMessage(text="/admin", user_id=GAME_MANAGER_ID, chat_id=GAME_MANAGER_ID)
    state = _fresh_state(GAME_MANAGER_ID)
    asyncio.run(admin_mod.cmd_admin_help(message, state))

    texts = [t for t, _pm, _rm in message.answers]
    assert any("нет доступных разделов" in (t or "") for t in texts)
    assert not any("Панель администратора" in (t or "") for t in texts)


# ── 08-06 Task 1 (D-13): notify_by_capability / capability_holders fan-out ─────────────────

def test_notify_fanout_reaches_capability_holders(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))

    bot = FakeBot()
    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))
    recipients = [chat_id for chat_id, _text in bot.sent]

    assert ADMIN_ID in recipients
    assert MANAGER_ID in recipients
    assert GAME_MANAGER_ID not in recipients
    assert sent == 2


def test_notify_fanout_falls_back_to_admins(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    # Nobody in `staff` holds moderate_reg at all (staff table is empty) -- fallback (T-08-31)
    # must still reach the bootstrap admin, never drop the notification.
    bot = FakeBot()
    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    assert [chat_id for chat_id, _t in bot.sent] == [ADMIN_ID]
    assert sent == 1


def test_notify_fanout_deduplicates(tmp_path):
    from handlers import admin_caps
    from handlers.admin_caps import role_caps_key

    _roles_ready(tmp_path)
    # Give game_manager's role_caps an EXTRA "moderate_reg" entry (on top of its default
    # moderate_game), so a person holding both reg_manager AND game_manager reaches
    # moderate_reg via two roles at once (D-08) -- must still get exactly one message.
    asyncio.run(db.set_setting(role_caps_key("game_manager"), "moderate_game\nmoderate_reg"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))

    bot = FakeBot()
    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    recipients = [chat_id for chat_id, _t in bot.sent]
    assert recipients.count(MANAGER_ID) == 1
    assert sent == len(recipients)


def test_notify_fanout_is_fail_soft(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))

    class FlakyBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            if chat_id == ADMIN_ID:
                raise RuntimeError("boom")
            self.sent.append((chat_id, text))

    bot = FlakyBot()
    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    # One recipient (ADMIN_ID) raised -- delivery to MANAGER_ID must not be blocked, and the
    # returned count reflects only the successful send.
    assert sent == 1
    assert bot.sent == [(MANAGER_ID, "hi")]


def test_notify_skips_disabled_role(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_setting("role_reg_manager_enabled", "off"))

    bot = FakeBot()
    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    recipients = [chat_id for chat_id, _t in bot.sent]
    assert MANAGER_ID not in recipients
    assert recipients == [ADMIN_ID]
    assert sent == 1


def test_technical_alert_sites_still_use_admin_ids():
    repo_root = Path(__file__).resolve().parent.parent
    for rel_path in ("services/sheets.py", "services/scheduler.py", "services/reminders.py"):
        source = (repo_root / rel_path).read_text(encoding="utf-8")
        assert "notify_by_capability" not in source
        assert source.count("for admin_id in config.ADMIN_IDS") == 1


def test_registration_and_payment_route_via_capability():
    repo_root = Path(__file__).resolve().parent.parent
    reg_src = (repo_root / "handlers/registration.py").read_text(encoding="utf-8")
    pay_src = (repo_root / "handlers/payment.py").read_text(encoding="utf-8")
    assert reg_src.count("notify_by_capability") >= 1
    assert pay_src.count("notify_by_capability") >= 1


# ── 08-06 Task 2 (D-13/D-14): delegate_questions row created once, before the fan-out ──────

def _count_questions():
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM delegate_questions").fetchone()[0]
    finally:
        conn.close()


def test_question_row_created_once_before_fanout(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))  # not moderate_reg

    bot = FakeBot()
    state = _fresh_state(STRANGER_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=STRANGER_ID, chat_id=STRANGER_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    # 3 potential recipients (admin bootstrap + reg_manager + game_manager, minus
    # game_manager which doesn't hold moderate_reg) -- still exactly ONE question row.
    assert _count_questions() == 1


def test_question_notification_carries_question_id(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))

    bot = FakeBot()
    state = _fresh_state(STRANGER_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=STRANGER_ID, chat_id=STRANGER_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    assert len(bot.sent) == 2  # ADMIN_ID + MANAGER_ID, both hold moderate_reg
    numbers = set(re.findall(r"Вопрос #<code>(\d+)</code>", "\n".join(t for _c, t in bot.sent)))
    assert len(numbers) == 1  # every copy carries the SAME question_id


def test_question_claim_race(tmp_path):
    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))

    first = asyncio.run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))
    second = asyncio.run(db.claim_question(qid, MANAGER_ID, "Менеджер Второй"))

    assert first is True
    assert second is False
    question = asyncio.run(db.get_question(qid))
    assert question["answered_by_name"] == "Админ Первый"
    assert question["answered_by"] == ADMIN_ID


def test_question_fanout_uses_moderate_reg(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(GAME_MANAGER_ID, "game_manager", ADMIN_ID))

    bot = FakeBot()
    state = _fresh_state(STRANGER_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=STRANGER_ID, chat_id=STRANGER_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    recipients = [chat_id for chat_id, _t in bot.sent]
    assert ADMIN_ID in recipients
    assert MANAGER_ID in recipients
    assert GAME_MANAGER_ID not in recipients


def test_question_delegate_reply_text_unchanged_on_success(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)  # ADMIN_ID (bootstrap) is always a moderate_reg holder

    bot = FakeBot()
    state = _fresh_state(STRANGER_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=STRANGER_ID, chat_id=STRANGER_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    texts = [t for t, _pm, _rm in message.answers]
    assert "Твой вопрос отправлен!" in texts


# ── 08-06 Task 3 (D-14): atomic claim in admin_reply_to_question ───────────────────────────

def _question_notification_text(qid, delegate_id, question_text="Когда дедлайн?"):
    """`FakeMessage.text` mirrors real aiogram behaviour: Telegram hands back the PLAIN
    rendered text for a reply_to_message -- HTML markup (<code>/<b>/<i>) is display-only,
    carried via `entities`, never present in `.text` itself. So this is the plain-text
    equivalent of the HTML `admin_text` process_question actually SENDS (post-Task-2):
    🆔/❓ markers (routing predicate) + the Вопрос #<id> marker (D-14 claim key)."""
    return (
        f"❓ Новый вопрос от ID: {delegate_id}:\n"
        f"🆔 {delegate_id}\n"
        f"🧾 Вопрос #{qid}\n\n"
        f"{question_text}\n\n"
        f"↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ."
    )


def _legacy_question_notification_text(delegate_id, question_text="Когда дедлайн?"):
    """Pre-Task-2 shape: no Вопрос #<id> marker at all (plain-text form, see above)."""
    return (
        f"❓ Новый вопрос от ID: {delegate_id}:\n"
        f"🆔 {delegate_id}\n\n"
        f"{question_text}\n\n"
        f"↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ."
    )


def test_reply_claims_question_and_sends_once(tmp_path):
    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    reply_to = FakeMessage(text=_question_notification_text(qid, STRANGER_ID))

    bot = FakeBot()
    result, event = dispatch_message(
        "Дедлайн 1 сентября", ADMIN_ID, reply_to=reply_to, bot=bot
    )

    assert result is not UNHANDLED
    assert (STRANGER_ID, "💬 <b>Ответ от организаторов:</b>\n\nДедлайн 1 сентября") in [
        (cid, t) for cid, t in bot.sent
    ]
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert "✅ Ответ отправлен пользователю." in reply_texts

    question = asyncio.run(db.get_question(qid))
    assert question["answer_text"] == "Дедлайн 1 сентября"
    assert question["answered_by"] == ADMIN_ID
    assert question["delivered_at"] is not None  # T-08-33 part D: stamped on success


def test_second_replier_is_rejected_with_winner_name(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    text = _question_notification_text(qid, STRANGER_ID)

    bot = FakeBot()
    dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=FakeMessage(text=text), bot=bot)
    sent_after_first = len(bot.sent)

    result, event = dispatch_message(
        "Дедлайн 2 сентября", MANAGER_ID, reply_to=FakeMessage(text=text), bot=bot
    )

    assert result is not UNHANDLED
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert any("уже ответил(а) Админ" in t for t in reply_texts)
    # Second responder's attempt must not reach the delegate at all.
    assert len(bot.sent) == sent_after_first
    assert not any(cid == STRANGER_ID and "2 сентября" in t for cid, t in bot.sent)


def test_reply_without_question_id_falls_back_to_legacy_path(tmp_path):
    _roles_ready(tmp_path)
    reply_to = FakeMessage(text=_legacy_question_notification_text(STRANGER_ID))

    bot = FakeBot()
    result, event = dispatch_message(
        "Дедлайн 1 сентября", ADMIN_ID, reply_to=reply_to, bot=bot
    )

    assert result is not UNHANDLED
    assert any(cid == STRANGER_ID for cid, _t in bot.sent)
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert "✅ Ответ отправлен пользователю." in reply_texts
    # No question row exists for this notification -- legacy path takes no claim.
    assert _count_questions() == 0


def test_reply_notifies_other_moderate_reg_holders(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    reply_to = FakeMessage(text=_question_notification_text(qid, STRANGER_ID))

    bot = FakeBot()
    dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=reply_to, bot=bot)

    recipients = [cid for cid, _t in bot.sent]
    assert MANAGER_ID in recipients  # holds moderate_reg, not the replier -- gets the notice
    assert ADMIN_ID not in recipients  # the replier never gets a self-notice
    assert STRANGER_ID in recipients  # the delegate still gets the actual answer


# ── QUICK T-08-33: distinguish permanent/transient delivery errors (B), let the SAME person
# retry a failed delivery (C), and detect/list "stuck" (claimed, never delivered) questions
# via delivered_at rather than answer_text (D). Variant A (releasing the claim on failure) is
# explicitly NOT implemented -- see 08-06-SUMMARY.md's "Known Limitations" + the backlog spec.

class _RaisingBot(FakeBot):
    """A FakeBot whose send_message raises a fixed exception `fail_times` times, then behaves
    like a normal FakeBot -- lets a test drive one claim through a failed attempt followed by
    a successful retry (or a second person's rejected attempt), all against the SAME bot."""

    def __init__(self, exc, fail_times=1):
        super().__init__()
        self._exc = exc
        self._remaining_fails = fail_times

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self._remaining_fails > 0:
            self._remaining_fails -= 1
            raise self._exc
        self.sent.append((chat_id, text))


def test_permanent_delivery_error_gives_no_retry_hint(tmp_path):
    from aiogram.exceptions import TelegramForbiddenError

    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    reply_to = FakeMessage(text=_question_notification_text(qid, STRANGER_ID))

    exc = TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
    bot = _RaisingBot(exc)
    result, event = dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=reply_to, bot=bot)

    assert result is not UNHANDLED
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert any("заблокировал бота" in t for t in reply_texts)
    assert not any("попробовать ещё раз" in t for t in reply_texts)

    question = asyncio.run(db.get_question(qid))
    assert question["answered_by"] == ADMIN_ID  # claim NOT released -- variant A rejected
    assert question["delivered_at"] is None


def test_permanent_bad_request_delivery_error_gives_no_retry_hint(tmp_path):
    from aiogram.exceptions import TelegramBadRequest

    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    reply_to = FakeMessage(text=_question_notification_text(qid, STRANGER_ID))

    exc = TelegramBadRequest(method=None, message="Bad Request: chat not found")
    bot = _RaisingBot(exc)
    result, event = dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=reply_to, bot=bot)

    assert result is not UNHANDLED
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert any("недоступен" in t or "заблокировал бота" in t for t in reply_texts)
    assert not any("попробовать ещё раз" in t for t in reply_texts)


def test_transient_delivery_error_suggests_retry(tmp_path):
    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    reply_to = FakeMessage(text=_question_notification_text(qid, STRANGER_ID))

    bot = _RaisingBot(TimeoutError("network blip"))
    result, event = dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=reply_to, bot=bot)

    assert result is not UNHANDLED
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert any("попробовать ещё раз" in t for t in reply_texts)
    assert not any("заблокировал бота" in t for t in reply_texts)

    question = asyncio.run(db.get_question(qid))
    assert question["answered_by"] == ADMIN_ID
    assert question["delivered_at"] is None


def test_same_person_retry_after_failed_delivery_succeeds(tmp_path):
    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    text = _question_notification_text(qid, STRANGER_ID)

    bot = _RaisingBot(TimeoutError("network blip"), fail_times=1)
    dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=FakeMessage(text=text), bot=bot)

    # Same person, same underlying claim, replying again -- the bot's one scripted failure is
    # already spent, so this attempt succeeds.
    result, event = dispatch_message(
        "Дедлайн 1 сентября (повтор)", ADMIN_ID, reply_to=FakeMessage(text=text), bot=bot
    )

    assert result is not UNHANDLED
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert "✅ Ответ отправлен пользователю." in reply_texts
    assert any(cid == STRANGER_ID for cid, _t in bot.sent)

    question = asyncio.run(db.get_question(qid))
    assert question["answered_by"] == ADMIN_ID
    assert question["delivered_at"] is not None
    assert question["answer_text"] == "Дедлайн 1 сентября (повтор)"


def test_different_manager_still_rejected_after_failed_delivery(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    text = _question_notification_text(qid, STRANGER_ID)

    bot = _RaisingBot(TimeoutError("network blip"), fail_times=1)
    dispatch_message("Дедлайн 1 сентября", ADMIN_ID, reply_to=FakeMessage(text=text), bot=bot)

    # A DIFFERENT manager (not the claimant) still gets the "already answered" rejection --
    # only the winner's own retry is allowed through (C's whole point).
    result, event = dispatch_message(
        "Дедлайн 2 сентября", MANAGER_ID, reply_to=FakeMessage(text=text), bot=bot
    )

    assert result is not UNHANDLED
    reply_texts = [t for t, _pm, _rm in event.answers]
    assert any("уже ответил(а) Админ" in t for t in reply_texts)
    assert not any(cid == STRANGER_ID for cid, _t in bot.sent)  # never delivered by either

    question = asyncio.run(db.get_question(qid))
    assert question["answered_by"] == ADMIN_ID
    assert question["delivered_at"] is None


def test_get_stuck_questions_returns_claimed_undelivered_only(tmp_path):
    _roles_ready(tmp_path)

    # Never claimed -- must not appear.
    asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))

    # Claimed, delivery failed (delivered_at never stamped) -- must appear.
    stuck_qid = asyncio.run(db.create_question(STRANGER_ID, "Где встреча?"))
    asyncio.run(db.claim_question(stuck_qid, ADMIN_ID, "Админ"))

    # Claimed AND delivered successfully with an EMPTY answer_text (mirrors a photo/voice/
    # sticker reply, whose `.text` is None) -- must NOT appear; proves the detector doesn't
    # fall back to answer_text emptiness (the exact pitfall the backlog spec calls out).
    delivered_empty_qid = asyncio.run(db.create_question(STRANGER_ID, "Фото площадки?"))
    asyncio.run(db.claim_question(delivered_empty_qid, ADMIN_ID, "Админ"))
    asyncio.run(db.set_question_answer(delivered_empty_qid, ""))

    rows = asyncio.run(db.get_stuck_questions())
    ids = {row["id"] for row in rows}

    assert ids == {stuck_qid}


def test_stuck_questions_screen_shows_claimed_undelivered(tmp_path):
    _roles_ready(tmp_path)
    qid = asyncio.run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    asyncio.run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))

    result, event = dispatch_callback("admin_stuck_questions", ADMIN_ID)

    assert result is not UNHANDLED
    assert str(STRANGER_ID) in event.message.text
    assert "Админ Первый" in event.message.text


def test_stuck_questions_screen_denied_for_stranger(tmp_path):
    _roles_ready(tmp_path)

    result, event = dispatch_callback("admin_stuck_questions", STRANGER_ID)

    assert event.answers == []


# ── 08-07 Task 2: registry round-trip the plan's own guide entries (Task 1) describe ───────
#
# 08-VALIDATION.md's Requirement -> Test Map names this behavior "-k settings_schema" for
# ROLE-02, but no earlier phase-8 plan actually shipped a test whose name contains that
# substring (checked: zero matches before this plan) -- this closes that gap for real,
# not just by naming a test to satisfy the gate below.

def test_settings_schema_role_caps_and_enabled_round_trip(tmp_path):
    _roles_ready(tmp_path)
    from settings_schema import get_setting_typed
    from handlers.admin_caps import role_caps_key, role_enabled_key

    # Unset -> registry default (D-09), not an empty list or None.
    assert asyncio.run(get_setting_typed(role_caps_key("reg_manager"))) == [
        "moderate_reg", "moderate_receipts",
    ]
    assert asyncio.run(get_setting_typed(role_enabled_key("reg_manager"))) == "on"

    # A real admin edit through the generic settings_edit flow (";"-separated, the mobile
    # Enter-sends-message workaround) round-trips through the SAME accessor resolve_capabilities
    # reads (D-05 no-cache: nothing intervenes between write and read).
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg;stats"))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "off"))
    assert asyncio.run(get_setting_typed(role_caps_key("reg_manager"))) == ["moderate_reg", "stats"]
    assert asyncio.run(get_setting_typed(role_enabled_key("reg_manager"))) == "off"


# ── 08-07 Task 2: final structural gates — old mechanism absent, new one complete ──────────
#
# T-08-35 (08-07-PLAN.md <threat_model>): a gate that reads source text could give a false
# "green" if a pure comment happens to contain the string it's checking for -- every gate
# below strips whole-comment lines (stripped first char == "#") BEFORE counting, so an
# explanatory comment ("the old is_admin filter is gone") can never itself trip or fake-pass
# a gate.

def _non_comment_source(path):
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()[:1] != "#"
    )


def test_gate_no_legacy_admin_check_remains():
    """D-01/D-03/T-08-18: the inline `not in config.ADMIN_IDS` checks and the `is_admin`
    filter (98 pre-phase call sites combined) are physically gone from handlers/admin.py --
    the only enforcement point is CapabilityMiddleware."""
    repo_root = Path(__file__).resolve().parent.parent
    source = _non_comment_source(repo_root / "handlers" / "admin.py")
    assert "not in config.ADMIN_IDS" not in source
    assert not re.search(r"\bis_admin\b", source)


def test_gate_single_capability_map():
    """D-01/D-15: exactly ONE dict maps event -> capability across the codebase -- ADMIN_CAPS
    in handlers/admin_caps.py. Both middleware (required_capability, called from
    CapabilityMiddleware) and menu assembly (_visible_menu_rows, via the same
    required_capability) resolve against it; handlers/admin.py never hand-rolls a second,
    parallel "button -> capability" dict (test_menu_has_no_second_map already checks 3
    literal pairs inside admin.py -- this gate is the stronger, whole-`handlers/`-directory
    version: no other file defines a module-level ADMIN_CAPS, and admin.py's own source never
    contains the identifier at all outside comments)."""
    repo_root = Path(__file__).resolve().parent.parent
    handlers_dir = repo_root / "handlers"

    definitions = [
        path.name for path in sorted(handlers_dir.glob("*.py"))
        if re.search(r"^ADMIN_CAPS\s*[:=]", _non_comment_source(path), re.MULTILINE)
    ]
    assert definitions == ["admin_caps.py"], (
        f"ADMIN_CAPS must be defined in exactly one module, found in: {definitions}"
    )

    admin_source = _non_comment_source(handlers_dir / "admin.py")
    assert "ADMIN_CAPS" not in admin_source  # never imported/copied by name into admin.py
    assert "required_capability" in admin_source  # resolves live against the one map instead

    caps_source = _non_comment_source(handlers_dir / "admin_caps.py")
    assert "ADMIN_CAPS.get(" in caps_source and "ADMIN_CAPS[" in caps_source


def test_gate_no_capability_cache():
    """D-05: no RAM cache in handlers/admin_caps.py -- staff is a local ~5-row table, not a
    networked source like services/allowlist.py (the explicitly-named anti-pattern this gate
    guards against reintroducing)."""
    repo_root = Path(__file__).resolve().parent.parent
    source = _non_comment_source(repo_root / "handlers" / "admin_caps.py")
    assert "lru_cache" not in source
    assert not re.search(r"^_?\w*cache\w*\s*[:=]", source, re.IGNORECASE | re.MULTILINE)
    assert not re.search(r"^(async def|def)\s+refresh_\w*\(", source, re.MULTILINE)


def test_gate_technical_alerts_stay_on_admin_ids():
    """D-13: the three technical-failure sites deliberately stay ADMIN_IDS-only -- a manager
    who only holds moderate_game can't fix a Google API quota problem, so routing this alert
    to them via notify_by_capability would only be noise. Same invariant as 08-06's
    test_technical_alert_sites_still_use_admin_ids, re-asserted here as one of this plan's own
    named final gates (08-07-PLAN.md Task 2 <behavior>), with the comment-line filter applied
    per T-08-35."""
    repo_root = Path(__file__).resolve().parent.parent
    for rel_path in ("services/sheets.py", "services/scheduler.py", "services/reminders.py"):
        source = _non_comment_source(repo_root / rel_path)
        assert "notify_by_capability" not in source
        assert source.count("for admin_id in config.ADMIN_IDS") == 1


# ── 08-07 Task 2 + 08-SECURITY.md WARNING #1: callback-namespace collision guard ────────────
#
# The audit's own recommendation (08-SECURITY.md "Suggested fix" #1): CapabilityMiddleware's
# _deny returns UNHANDLED for a capability-less caller -- including CallbackQuery events,
# which were NEVER identity-filtered pre-phase-8 (is_admin only ever wrapped @router.message).
# No live collision exists today (verified independently below, matching the audit's own
# finding), but nothing previously stood between a future phase (9 gamification, 12 check-in)
# adding a sibling-router callback prefix and silently reopening this. This walks the three
# sibling routers' registered callback_data literals/prefixes and asserts none of them can
# ever match a key in the admin namespace.

_CALLBACK_LITERAL_RE = re.compile(r'F\.data\s*==\s*"([^"]+)"')
_CALLBACK_PREFIX_RE = re.compile(r'F\.data\.startswith\("([^"]+)"\)')


def _sibling_callback_keys(source):
    """Same literal/prefix extraction shape as _callback_keys_from_line above, applied to a
    whole sibling-router file rather than one admin.py decorator line at a time."""
    keys = []
    for line in source.splitlines():
        if line.strip()[:1] == "#":
            continue
        keys.extend(_CALLBACK_LITERAL_RE.findall(line))
        keys.extend(m + "*" for m in _CALLBACK_PREFIX_RE.findall(line))
    return keys


def _keys_collide(key_a, key_b):
    """Two ADMIN_CAPS-style keys (bare exact string, or "prefix*") collide if some concrete
    callback_data value could match both -- exact==exact, exact-inside-prefix either
    direction, or two prefixes where one contains the other."""
    a_prefix, a_is_prefix = (key_a[:-1], True) if key_a.endswith("*") else (key_a, False)
    b_prefix, b_is_prefix = (key_b[:-1], True) if key_b.endswith("*") else (key_b, False)
    if not a_is_prefix and not b_is_prefix:
        return key_a == key_b
    if a_is_prefix and not b_is_prefix:
        return key_b.startswith(a_prefix)
    if b_is_prefix and not a_is_prefix:
        return key_a.startswith(b_prefix)
    return a_prefix.startswith(b_prefix) or b_prefix.startswith(a_prefix)


def test_gate_no_callback_namespace_collision_with_admin():
    from handlers.admin_caps import ADMIN_CAPS

    repo_root = Path(__file__).resolve().parent.parent
    admin_keys = [k for k in ADMIN_CAPS if not k.startswith(("cmd:", "state:", "special:"))]

    collisions = []
    for rel_path in ("handlers/payment.py", "handlers/registration.py", "handlers/user_actions.py"):
        source = (repo_root / rel_path).read_text(encoding="utf-8")
        for sibling_key in _sibling_callback_keys(source):
            for admin_key in admin_keys:
                if _keys_collide(admin_key, sibling_key):
                    collisions.append((rel_path, sibling_key, admin_key))

    assert not collisions, (
        "Callback-data collision between the admin namespace and a sibling router -- a "
        "forged admin-shaped callback_data from a capability-less caller would fall through "
        f"to this handler (08-SECURITY.md WARNING #1): {collisions}"
    )


def test_gate_validation_map_is_covered():
    """08-VALIDATION.md's Requirement -> Test Map names 11 -k selectors for phase 8's
    behaviors; every one of them must resolve to at least one real test function in this
    module (checked by substring, matching how the map's own Automated Command column is
    phrased as `-k <name>`)."""
    expected_names = [
        "capabilities", "completeness", "middleware_denies", "middleware_allows",
        "deny_response", "bootstrap_compat", "staff_crud", "settings_schema",
        "roles_group_ui", "question_claim_race", "notify_fanout",
    ]
    all_test_names = [name for name in globals() if name.startswith("test_")]
    missing = [
        expected for expected in expected_names
        if not any(expected in actual for actual in all_test_names)
    ]
    assert not missing, f"08-VALIDATION.md map names with no matching test function: {missing}"
