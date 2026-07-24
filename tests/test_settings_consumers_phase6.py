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
