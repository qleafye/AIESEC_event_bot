"""Phase 6 plan 06-03 (REG-02): consumer parse-equivalence + behavior-preservation tests.

Proves two things for the three migrated consumers (services/reminders.py,
services/scheduler.py, keyboards/builders.py):

1. Oracle equivalence — `settings_schema.get_setting_typed` resolves int/date/list settings
   to byte-for-byte the same typed value the consumer's own pre-migration pure-helper parse
   (`_reminder_interval`, `_parse_schedule_dt`, the source_options splitlines idiom) produces,
   across None/empty/garbage/valid inputs (T-06-09).
2. Consumer wiring — after migration, the CONSUMER function itself resolves its setting via
   `get_setting_typed` rather than re-implementing the parse inline (proven by monkeypatching
   the module-level `get_setting_typed` name and asserting it was actually called).

pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async helper
is driven via asyncio.run() and config.DB_PATH points at a tmp_path file (same scaffold as
tests/test_settings_groups_c0x.py).
"""
import asyncio

from config import config
from database import db
from database.db import get_setting, set_setting, delete_setting


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_consumers_phase6.db")
    asyncio.run(db.init_db())


def _flat_button_texts(kb):
    """Extract button texts from a ReplyKeyboardMarkup, row-major flattened."""
    return [btn.text for row in kb.keyboard for btn in row]


# ── Oracle equivalence: pending_reminder_interval (int) ──────────────────────────────

def test_reminders_interval_via_registry_matches_oracle(tmp_path):
    _db_ready(tmp_path)
    from settings_schema import get_setting_typed
    from services.reminders import _reminder_interval

    for raw in [None, "900", "0", "abc"]:
        asyncio.run(delete_setting("pending_reminder_interval"))
        if raw is not None:
            asyncio.run(set_setting("pending_reminder_interval", raw))

        typed = asyncio.run(get_setting_typed("pending_reminder_interval"))
        oracle = _reminder_interval(asyncio.run(get_setting("pending_reminder_interval")))
        assert typed == oracle, f"mismatch for raw={raw!r}: typed={typed!r} oracle={oracle!r}"


# ── Oracle equivalence: payment_deadline (date) ───────────────────────────────────────

def test_scheduler_date_via_registry_matches_oracle(tmp_path):
    _db_ready(tmp_path)
    from settings_schema import get_setting_typed
    from services.scheduler import _parse_schedule_dt

    for raw in [None, "garbage", "15.08.2026 23:59"]:
        asyncio.run(delete_setting("payment_deadline"))
        if raw is not None:
            asyncio.run(set_setting("payment_deadline", raw))

        typed = asyncio.run(get_setting_typed("payment_deadline"))
        oracle = _parse_schedule_dt(asyncio.run(get_setting("payment_deadline")))
        assert typed == oracle, f"mismatch for raw={raw!r}: typed={typed!r} oracle={oracle!r}"


# ── Behavior preservation: get_source_kb (list) ───────────────────────────────────────

def test_source_kb_unchanged(tmp_path):
    _db_ready(tmp_path)
    from keyboards.builders import get_source_kb, DEFAULT_SOURCE_OPTIONS

    asyncio.run(delete_setting("source_options"))
    kb_unset = asyncio.run(get_source_kb())
    assert _flat_button_texts(kb_unset) == DEFAULT_SOURCE_OPTIONS

    asyncio.run(set_setting("source_options", "a\nb"))
    kb_custom = asyncio.run(get_source_kb())
    assert _flat_button_texts(kb_custom) == ["a", "b"]


def test_source_kb_default_includes_blogger_before_other(tmp_path):
    """2026-08-17: «Узнал от блогера» is a stock source option (Instagram bloggers post
    without a direct reg link, so this is the only way to attribute those registrations).
    It must be present by default and «Другое» must stay last."""
    _db_ready(tmp_path)
    from keyboards.builders import get_source_kb, DEFAULT_SOURCE_OPTIONS

    assert "Узнал от блогера" in DEFAULT_SOURCE_OPTIONS
    assert DEFAULT_SOURCE_OPTIONS[-1] == "Другое"
    assert DEFAULT_SOURCE_OPTIONS.index("Узнал от блогера") < DEFAULT_SOURCE_OPTIONS.index("Другое")

    asyncio.run(delete_setting("source_options"))
    texts = _flat_button_texts(asyncio.run(get_source_kb()))
    assert "Узнал от блогера" in texts and texts[-1] == "Другое"


# ── Consumer wiring: pending_reminder_loop must resolve the interval via get_setting_typed ──

class _StopLoop(Exception):
    """Sentinel raised from a patched asyncio.sleep to escape the infinite reminder loop
    after exactly one iteration, without altering services/reminders.py's structure."""


class _FakeBot:
    async def send_message(self, *args, **kwargs):
        pass


def test_reminders_loop_reads_interval_via_registry(tmp_path):
    _db_ready(tmp_path)
    import services.reminders as reminders_mod

    calls = []

    async def fake_get_setting_typed(key):
        calls.append(key)
        return 1800

    async def fake_sleep(_seconds):
        raise _StopLoop()

    had_attr = hasattr(reminders_mod, "get_setting_typed")
    orig_get_setting_typed = getattr(reminders_mod, "get_setting_typed", None)
    orig_sleep = reminders_mod.asyncio.sleep
    reminders_mod.get_setting_typed = fake_get_setting_typed
    reminders_mod.asyncio.sleep = fake_sleep
    try:
        raised = False
        try:
            asyncio.run(reminders_mod.pending_reminder_loop(_FakeBot()))
        except _StopLoop:
            raised = True
        assert raised, "pending_reminder_loop did not reach the sleep call (unexpected)"
    finally:
        reminders_mod.asyncio.sleep = orig_sleep
        if had_attr:
            reminders_mod.get_setting_typed = orig_get_setting_typed
        else:
            del reminders_mod.get_setting_typed

    assert "pending_reminder_interval" in calls, (
        "pending_reminder_loop did not resolve pending_reminder_interval via "
        "get_setting_typed — still using the old local parse"
    )


# ── Consumer wiring: get_source_kb must resolve source_options via get_setting_typed ──

def test_source_kb_reads_via_registry(tmp_path):
    _db_ready(tmp_path)
    import keyboards.builders as builders_mod

    calls = []

    async def fake_get_setting_typed(key):
        calls.append(key)
        return ["x", "y"]

    had_attr = hasattr(builders_mod, "get_setting_typed")
    orig_get_setting_typed = getattr(builders_mod, "get_setting_typed", None)
    builders_mod.get_setting_typed = fake_get_setting_typed
    try:
        asyncio.run(builders_mod.get_source_kb())
    finally:
        if had_attr:
            builders_mod.get_setting_typed = orig_get_setting_typed
        else:
            del builders_mod.get_setting_typed

    assert "source_options" in calls, (
        "get_source_kb did not resolve source_options via get_setting_typed — still "
        "using the old local parse"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════
# Phase 6 plan 06-06 (REG-02): reg/payment/scheduler feature-switch gate migration
#
# Every test below drives the REAL gate/helper function through a tmp DB (not a hand-copied
# reimplementation) and compares the result to an oracle computed inline from the OLD
# `get_setting(k) or "<default>"` / bare `== "on"` idiom -- proving byte-for-byte equivalence
# for None/""/on/off (or the enum's own option set) inputs (the empty-string case is the D-15
# trap every one of these tests is required to cover).
#
# Value-equivalence alone can't force RED here: D-15/06-04 already proved the registry's enum
# default is byte-identical to the old idiom, so the numeric/boolean RESULT is the same whether
# the gate is wired to get_setting_typed or not. RED is driven by (a) monkeypatching the target
# module's `get_setting_typed` name and asserting it was actually invoked with the expected
# key(s), or (b) `inspect.getsource` source-checks against the harder-to-isolate call sites
# inside `finalize_registration` (mirrors the "replicate the slice, don't drive the whole
# function" test style already used by tests/test_registration_phase5.py's `_finalize_row`).
# ═══════════════════════════════════════════════════════════════════════════════════════


def test_edu_conditional_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import handlers.registration as reg_mod

    async def go():
        for raw in [None, "", "on", "off"]:
            await delete_setting("edu_conditional")
            if raw is not None:
                await set_setting("edu_conditional", raw)
            steps = await reg_mod._get_enabled_steps({})  # studying=False (no education_status)
            edu_conditional_on = (raw or "on") == "on"
            if edu_conditional_on:
                assert "university" not in steps, f"raw={raw!r}: university should be skipped"
                assert "course" not in steps, f"raw={raw!r}: course should be skipped"
            else:
                assert "university" in steps, f"raw={raw!r}: university should be shown"
                assert "course" in steps, f"raw={raw!r}: course should be shown"
    asyncio.run(go())

    # Wiring (RED before Task 2): _get_enabled_steps must resolve edu_conditional via
    # get_setting_typed, not the local `get_setting(...) or "on"` idiom.
    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "on"

    had_attr = hasattr(reg_mod, "get_setting_typed")
    orig = getattr(reg_mod, "get_setting_typed", None)
    reg_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(reg_mod._get_enabled_steps({}))
    finally:
        if had_attr:
            reg_mod.get_setting_typed = orig
        else:
            del reg_mod.get_setting_typed
    assert "edu_conditional" in calls, (
        "_get_enabled_steps did not resolve edu_conditional via get_setting_typed"
    )


def test_party_enabled_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import handlers.registration as reg_mod

    async def go_fork():
        for fork_raw in [None, "", "on", "off"]:
            for enabled_raw in [None, "", "on", "off"]:
                await delete_setting("party_fork_question")
                await delete_setting("party_enabled")
                if fork_raw is not None:
                    await set_setting("party_fork_question", fork_raw)
                if enabled_raw is not None:
                    await set_setting("party_enabled", enabled_raw)
                result = await reg_mod._should_show_fork(None, None, False)
                oracle = (fork_raw or "off") == "on" and (enabled_raw or "off") == "on"
                assert result == oracle, (
                    f"mismatch fork_raw={fork_raw!r} enabled_raw={enabled_raw!r}"
                )
    asyncio.run(go_fork())

    async def go_progress():
        for raw in [None, "", "on", "off"]:
            await delete_setting("reg_show_progress")
            if raw is not None:
                await set_setting("reg_show_progress", raw)
            text = await reg_mod._progress(2, 5)
            expected = "(2/5) " if (raw or "off") == "on" else ""
            assert text == expected, f"reg_show_progress mismatch raw={raw!r}"
    asyncio.run(go_progress())

    # Wiring (RED before Task 2): both fork-family gates + progress must resolve via
    # get_setting_typed. Force both settings "on" so _should_show_fork doesn't short-circuit
    # before reaching the party_enabled check.
    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "on"

    had_attr = hasattr(reg_mod, "get_setting_typed")
    orig = getattr(reg_mod, "get_setting_typed", None)
    reg_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(reg_mod._should_show_fork(None, None, False))
        asyncio.run(reg_mod._progress(1, 2))
    finally:
        if had_attr:
            reg_mod.get_setting_typed = orig
        else:
            del reg_mod.get_setting_typed
    assert "party_fork_question" in calls, "_should_show_fork did not resolve party_fork_question via get_setting_typed"
    assert "party_enabled" in calls, "_should_show_fork did not resolve party_enabled via get_setting_typed"
    assert "reg_show_progress" in calls, "_progress did not resolve reg_show_progress via get_setting_typed"


def test_payment_enabled_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import handlers.payment as pay_mod
    from database.db import add_user

    async def go():
        await add_user({"telegram_id": 800001, "full_name": "A B", "registration_date": "x"})
        await set_setting("payment_requisites", "Card 1234")
        for raw in [None, "", "on", "off"]:
            await delete_setting("payment_enabled")
            if raw is not None:
                await set_setting("payment_enabled", raw)
            result = await pay_mod.should_offer_receipt_upload(800001)
            oracle = (raw or "off") == "on"
            assert result == oracle, f"payment_enabled mismatch raw={raw!r}"
    asyncio.run(go())

    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "off"

    had_attr = hasattr(pay_mod, "get_setting_typed")
    orig = getattr(pay_mod, "get_setting_typed", None)
    pay_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(pay_mod.should_offer_receipt_upload(800001))
    finally:
        if had_attr:
            pay_mod.get_setting_typed = orig
        else:
            del pay_mod.get_setting_typed
    assert "payment_enabled" in calls, (
        "should_offer_receipt_upload did not resolve payment_enabled via get_setting_typed"
    )


def test_payment_reminders_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import services.scheduler as sched_mod
    from database.db import add_user
    import aiosqlite

    class _FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text))

    async def go_reminder():
        await add_user({"telegram_id": 810001, "full_name": "A B", "registration_date": "x"})
        fake_bot = _FakeBot()
        sched_mod._bot = fake_bot
        try:
            for raw in [None, "", "on", "off"]:
                fake_bot.sent.clear()
                await delete_setting("payment_reminders_enabled")
                if raw is not None:
                    await set_setting("payment_reminders_enabled", raw)
                await sched_mod.send_payment_reminder(810001)
                oracle_fires = (raw or "on") == "on"
                assert bool(fake_bot.sent) == oracle_fires, f"send_payment_reminder mismatch raw={raw!r}"
        finally:
            sched_mod._bot = None
    asyncio.run(go_reminder())

    async def go_overdue():
        await add_user({"telegram_id": 810002, "full_name": "C D", "registration_date": "x"})
        await set_setting("payment_deadline", "01.01.2020 00:00")
        fake_bot = _FakeBot()
        sched_mod._bot = fake_bot
        try:
            for raw in [None, "", "on", "off"]:
                async with aiosqlite.connect(config.DB_PATH) as dbconn:
                    await dbconn.execute(
                        "UPDATE users SET payment_status='not_paid', payment_option='A' WHERE telegram_id=?",
                        (810002,),
                    )
                    await dbconn.commit()
                fake_bot.sent.clear()
                await delete_setting("payment_reminders_enabled")
                if raw is not None:
                    await set_setting("payment_reminders_enabled", raw)
                await sched_mod.sweep_payment_overdue()
                oracle_fires = (raw or "on") == "on"
                assert bool(fake_bot.sent) == oracle_fires, f"sweep_payment_overdue mismatch raw={raw!r}"
        finally:
            sched_mod._bot = None
    asyncio.run(go_overdue())

    # Wiring (scheduler.py already imports get_setting_typed for payment_deadline -- confirm
    # the payment_reminders_enabled fire-time gates route through the SAME name now, not the
    # old `get_setting(...) or "on"` idiom).
    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "on"

    orig = sched_mod.get_setting_typed
    fake_bot2 = _FakeBot()
    sched_mod._bot = fake_bot2
    sched_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(sched_mod.send_payment_reminder(810001))
    finally:
        sched_mod.get_setting_typed = orig
        sched_mod._bot = None
    assert "payment_reminders_enabled" in calls, (
        "send_payment_reminder did not resolve payment_reminders_enabled via get_setting_typed"
    )


def test_reg_bonus_enabled_equiv(tmp_path):
    _db_ready(tmp_path)
    import handlers.registration as reg_mod
    import handlers.reg_schema as reg_schema_mod  # 13-02 (REFAC-02): send_completion_and_bonus lives here now

    sent = []

    class _FakeBot2:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(("text", chat_id, text))

        async def send_document(self, chat_id, doc, **kwargs):
            sent.append(("doc", chat_id, doc))

        async def send_photo(self, chat_id, photo, **kwargs):
            sent.append(("photo", chat_id, photo))

    async def go():
        await set_setting("reg_bonus_photo_file_id", "PHOTOID123")
        for raw in [None, "", "on", "off"]:
            sent.clear()
            await delete_setting("reg_bonus_enabled")
            if raw is not None:
                await set_setting("reg_bonus_enabled", raw)
            await reg_mod.send_completion_and_bonus(_FakeBot2(), 820001, with_menu=False)
            bonus_sent = any(item[0] == "photo" for item in sent)
            oracle = raw == "on"  # bare `== "on"` idiom, no `or` -- None/""/off/anything -> False
            assert bonus_sent == oracle, f"reg_bonus_enabled mismatch raw={raw!r}"
    asyncio.run(go())

    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "off"

    had_attr = hasattr(reg_schema_mod, "get_setting_typed")
    orig = getattr(reg_schema_mod, "get_setting_typed", None)
    reg_schema_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(reg_mod.send_completion_and_bonus(_FakeBot2(), 820001, with_menu=False))
    finally:
        if had_attr:
            reg_schema_mod.get_setting_typed = orig
        else:
            del reg_schema_mod.get_setting_typed
    assert "reg_bonus_enabled" in calls, (
        "send_completion_and_bonus did not resolve reg_bonus_enabled via get_setting_typed"
    )


def test_is_module_enabled_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import handlers.registration as reg_mod
    import handlers.reg_schema as reg_schema_mod  # 13-02 (REFAC-02): _is_module_enabled lives here now

    async def go():
        for key in ("payment_enabled", "consent_enabled"):
            for raw in [None, "", "on", "off"]:
                await delete_setting(key)
                if raw is not None:
                    await set_setting(key, raw)
                result = await reg_mod._is_module_enabled(key)
                oracle = raw == "on"  # None/absent/""/off/anything-but-"on" -> False
                assert result == oracle, f"_is_module_enabled mismatch key={key} raw={raw!r}"
    asyncio.run(go())

    # Wiring (BLOCKER-2, RED before Task 2): the helper BODY must call get_setting_typed(key)
    # directly -- this covers all 4 call sites (consent_enabled/payment_enabled) at once.
    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "off"

    had_attr = hasattr(reg_schema_mod, "get_setting_typed")
    orig = getattr(reg_schema_mod, "get_setting_typed", None)
    reg_schema_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(reg_mod._is_module_enabled("payment_enabled"))
        asyncio.run(reg_mod._is_module_enabled("consent_enabled"))
    finally:
        if had_attr:
            reg_schema_mod.get_setting_typed = orig
        else:
            del reg_schema_mod.get_setting_typed
    assert calls == ["payment_enabled", "consent_enabled"], (
        f"_is_module_enabled did not resolve both keys via get_setting_typed, got calls={calls!r}"
    )


def test_registration_mode_and_reg_university_mode_equiv(tmp_path):
    _db_ready(tmp_path)
    import inspect
    import handlers.registration as reg_mod
    # Phase 13 REFAC (13-03): process_full_name moved to handlers/reg_steps.py -- its own
    # calls to finalize_registration/_get_enabled_steps resolve via reg_steps's OWN module
    # globals (bound at import time), not reg_mod's, so patch targets follow the function's
    # real home. _ask_step stays patched on reg_mod: it's called from _ask_step_or_recall,
    # which still lives in handlers/registration.py (unmoved engine helper).
    import handlers.reg_steps as reg_steps_mod

    class _Msg:
        def __init__(self, text="Иван Иванов"):
            self.text = text

        async def answer(self, *a, **k):
            return None

    class _St:
        def __init__(self):
            self.data = {}

        async def update_data(self, **kw):
            self.data.update(kw)

        async def get_data(self):
            return dict(self.data)

    async def go_full_name():
        finalize_calls = []
        ask_calls = []

        async def fake_finalize(message, state, bot):
            finalize_calls.append(1)

        async def fake_get_enabled(data):
            return ["age"]  # non-empty so the "full" branch proceeds to _ask_step

        async def fake_ask_step(step_key, message, state, step, total):
            ask_calls.append(step_key)

        orig_finalize = reg_steps_mod.finalize_registration
        orig_get_enabled = reg_steps_mod._get_enabled_steps
        orig_ask_step = reg_mod._ask_step
        reg_steps_mod.finalize_registration = fake_finalize
        reg_steps_mod._get_enabled_steps = fake_get_enabled
        reg_mod._ask_step = fake_ask_step
        try:
            # Phase 7 (07-01, Task 2): the registration_mode raw-read this loop used to pin was
            # REMOVED from process_full_name entirely (not just migrated to get_setting_typed) —
            # the short/full fork now lives structurally in `_get_enabled_steps` (resolved once,
            # earlier, via `_resolve_track` at flow start), not as a live re-read here. With
            # `_get_enabled_steps` mocked non-empty, process_full_name must reach `_ask_step`
            # for EVERY registration_mode raw value — there is no branch left to gate on it.
            for raw in [None, "", "full", "short"]:
                finalize_calls.clear()
                ask_calls.clear()
                await delete_setting("registration_mode")
                if raw is not None:
                    await set_setting("registration_mode", raw)
                await reg_steps_mod.process_full_name(_Msg(), _St(), bot=object())
                assert ask_calls, f"expected _ask_step to run regardless of raw={raw!r}"
                assert not finalize_calls
        finally:
            reg_steps_mod.finalize_registration = orig_finalize
            reg_steps_mod._get_enabled_steps = orig_get_enabled
            reg_mod._ask_step = orig_ask_step
    asyncio.run(go_full_name())

    class _FakeChat:
        def __init__(self, cid):
            self.id = cid

    class _CaptureMsg:
        def __init__(self):
            self.chat = _FakeChat(830001)
            self.markups = []

        async def answer(self, text=None, reply_markup=None, **kwargs):
            self.markups.append(reply_markup)

        async def answer_photo(self, *a, **k):
            pass

    class _St2:
        def __init__(self):
            self.data = {"participant_type": "full"}

        async def get_data(self):
            return dict(self.data)

        async def update_data(self, **kw):
            self.data.update(kw)

        async def set_state(self, *a, **k):
            pass

    async def go_university():
        for raw in [None, "", "list", "text"]:
            await delete_setting("reg_university_mode")
            if raw is not None:
                await set_setting("reg_university_mode", raw)
            msg = _CaptureMsg()
            await reg_mod._ask_step("university", msg, _St2(), 1, 1)
            oracle_text_mode = (raw or "text") == "text"
            markup = msg.markups[-1]
            texts = [btn.text for row in markup.keyboard for btn in row]
            if oracle_text_mode:
                assert "Пропустить" in texts, f"expected skip-kb (text mode) for raw={raw!r}"
            else:
                assert "Пропустить" not in texts, f"expected list-mode kb for raw={raw!r}"
    asyncio.run(go_university())

    # Wiring (RED before Task 2 of 06-06): originally pinned that BOTH registration_mode
    # read-sites (bare-raw in process_full_name, `or "short"` in finalize_registration feeding
    # _decide_status) resolved via get_setting_typed. Phase 7 (07-01, Task 2) went one step
    # further for process_full_name and REMOVED that read-site entirely — the short/full fork
    # no longer lives there at all (see the "Phase 7" comment above and _resolve_track), so
    # asserting its continued presence would pin exactly the code this phase intentionally
    # deleted. finalize_registration's own registration_mode read (feeding _decide_status's
    # reg_mode param, still used for the full/None branch) is untouched by Phase 7 and keeps
    # the original assertion.
    process_src = inspect.getsource(reg_steps_mod.process_full_name)
    finalize_src = inspect.getsource(reg_mod.finalize_registration)
    assert 'get_setting_typed("registration_mode")' not in process_src, (
        "process_full_name should no longer read registration_mode at all (Phase 7, 07-01 Task 2 "
        "moved the short/full fork to _resolve_track at flow start)"
    )
    assert 'get_setting_typed("registration_mode")' in finalize_src, (
        "finalize_registration does not resolve registration_mode via get_setting_typed"
    )

    calls = []

    async def fake_typed(key):
        calls.append(key)
        return "text"

    had_attr = hasattr(reg_mod, "get_setting_typed")
    orig_typed = getattr(reg_mod, "get_setting_typed", None)
    reg_mod.get_setting_typed = fake_typed
    try:
        asyncio.run(reg_mod._ask_step("university", _CaptureMsg(), _St2(), 1, 1))
    finally:
        if had_attr:
            reg_mod.get_setting_typed = orig_typed
        else:
            del reg_mod.get_setting_typed
    assert "reg_university_mode" in calls, (
        "_ask_step's university branch does not resolve reg_university_mode via get_setting_typed"
    )


def test_full_approval_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import inspect
    import handlers.registration as reg_mod
    from settings_schema import get_setting_typed

    for raw in [None, "", "manual", "auto"]:
        asyncio.run(delete_setting("full_approval"))
        if raw is not None:
            asyncio.run(set_setting("full_approval", raw))
        typed = asyncio.run(get_setting_typed("full_approval"))
        oracle = raw or "manual"
        assert typed == oracle, f"full_approval mismatch raw={raw!r}"

    # BLOCKER-1, RED before Task 2: finalize_registration must feed _decide_status via
    # get_setting_typed("full_approval"), not `get_setting("full_approval") or "manual"`.
    finalize_src = inspect.getsource(reg_mod.finalize_registration)
    assert 'get_setting_typed("full_approval")' in finalize_src, (
        "finalize_registration does not resolve full_approval via get_setting_typed (BLOCKER-1)"
    )


def test_short_approval_and_party_approval_equiv(tmp_path):
    _db_ready(tmp_path)
    import inspect
    import handlers.registration as reg_mod
    from settings_schema import get_setting_typed

    for raw in [None, "", "manual", "auto"]:
        asyncio.run(delete_setting("short_approval"))
        if raw is not None:
            asyncio.run(set_setting("short_approval", raw))
        typed = asyncio.run(get_setting_typed("short_approval"))
        oracle = raw or "auto"
        assert typed == oracle, f"short_approval mismatch raw={raw!r}"

    for raw in [None, "", "manual", "auto"]:
        asyncio.run(delete_setting("party_approval"))
        if raw is not None:
            asyncio.run(set_setting("party_approval", raw))
        typed = asyncio.run(get_setting_typed("party_approval"))
        # party_approval's live read site is RAW (no inline `or`) -- _decide_status applies
        # its own `party_setting or "manual"` fallback downstream. The enum registry default
        # ("manual") matches that fallback exactly (see test_raw_read_sites_preserved for the
        # branch-level proof), so the resolved value is provably interchangeable with raw.
        oracle = raw if raw else "manual"
        assert typed == oracle, f"party_approval mismatch raw={raw!r}"

        # _decide_status's party branch must produce the SAME status whether fed the raw
        # value or the registry-resolved value.
        old_status = reg_mod._decide_status("full", "manual", "auto", "party_overnight", raw)
        new_status = reg_mod._decide_status("full", "manual", "auto", "party_overnight", typed)
        assert old_status == new_status, f"party_approval branch changed for raw={raw!r}"

    finalize_src = inspect.getsource(reg_mod.finalize_registration)
    assert 'get_setting_typed("short_approval")' in finalize_src, (
        "finalize_registration does not resolve short_approval via get_setting_typed"
    )
    assert 'get_setting_typed("party_approval")' in finalize_src, (
        "finalize_registration does not resolve party_approval via get_setting_typed"
    )


def test_pending_notify_mode_gate_equiv(tmp_path):
    _db_ready(tmp_path)
    import inspect
    import handlers.registration as reg_mod
    from settings_schema import get_setting_typed

    for raw in [None, "", "instant", "batched"]:
        asyncio.run(delete_setting("pending_notify_mode"))
        if raw is not None:
            asyncio.run(set_setting("pending_notify_mode", raw))
        typed = asyncio.run(get_setting_typed("pending_notify_mode"))
        old_is_instant = (raw or "batched") == "instant"
        new_is_instant = typed == "instant"
        assert old_is_instant == new_is_instant, f"pending_notify_mode mismatch raw={raw!r}"

    finalize_src = inspect.getsource(reg_mod.finalize_registration)
    assert 'get_setting_typed("pending_notify_mode")' in finalize_src, (
        "finalize_registration does not resolve pending_notify_mode via get_setting_typed"
    )


def test_raw_read_sites_preserved(tmp_path):
    """Both documented RAW-read sites (registration_mode in process_full_name, party_approval
    in finalize_registration) were migrated to get_setting_typed -- proven safe because, in both
    cases, the registry's enum default is byte-identical to what the downstream branch already
    treats None/absent as (see test_registration_mode_and_reg_university_mode_equiv /
    test_short_approval_and_party_approval_equiv for the full matrices).

    Phase 7 (07-01, Task 2) subsequently REMOVED the registration_mode site from
    process_full_name entirely (see that test's updated docstring) -- Site 1's equivalence
    loop below still documents that migrating the RAW read to get_setting_typed would have
    been branch-safe, it just no longer describes live code in process_full_name."""
    _db_ready(tmp_path)
    import inspect
    import handlers.registration as reg_mod
    # Phase 13 REFAC (13-03): process_full_name moved to handlers/reg_steps.py.
    import handlers.reg_steps as reg_steps_mod
    from settings_schema import get_setting_typed

    # Site 1: registration_mode raw read (process_full_name) -- branch is `mode != "full"`.
    # Migrating to get_setting_typed (default "short") cannot change this branch: None and ""
    # both fail `!= "full"` before AND after (default "short" != "full" too).
    for raw in [None, "", "full", "short"]:
        asyncio.run(delete_setting("registration_mode"))
        if raw is not None:
            asyncio.run(set_setting("registration_mode", raw))
        old_branch = raw != "full"
        typed = asyncio.run(get_setting_typed("registration_mode"))
        new_branch = typed != "full"
        assert old_branch == new_branch, f"registration_mode raw-site branch changed for raw={raw!r}"

    # Site 2: party_approval raw read (finalize_registration) -- feeds _decide_status's own
    # `party_setting or "manual"` fallback; get_setting_typed's default ("manual") is identical
    # to that fallback, so passing the resolved value instead of raw cannot change the branch.
    for raw in [None, "", "manual", "auto"]:
        asyncio.run(delete_setting("party_approval"))
        if raw is not None:
            asyncio.run(set_setting("party_approval", raw))
        old_status = reg_mod._decide_status("full", "manual", "auto", "party_overnight", raw)
        typed = asyncio.run(get_setting_typed("party_approval"))
        new_status = reg_mod._decide_status("full", "manual", "auto", "party_overnight", typed)
        assert old_status == new_status, f"party_approval raw-site branch changed for raw={raw!r}"

    # Site 2 was provably safe to migrate (not merely "leave raw with a comment") -- pin that
    # decision at the source level. Site 1's equivalent pin was removed here: Phase 7 (07-01,
    # Task 2) deleted the registration_mode read from process_full_name outright (the short/full
    # fork moved to _resolve_track at flow start), so asserting its presence would contradict
    # the currently-correct source.
    process_src = inspect.getsource(reg_steps_mod.process_full_name)
    finalize_src = inspect.getsource(reg_mod.finalize_registration)
    assert 'get_setting_typed("registration_mode")' not in process_src, (
        "process_full_name should no longer read registration_mode at all (Phase 7, 07-01 Task 2)"
    )
    assert 'get_setting_typed("party_approval")' in finalize_src, (
        "finalize_registration should migrate the raw party_approval read (RAW-read rule: "
        "_decide_status's own `party_setting or \"manual\"` fallback matches the enum default)"
    )
