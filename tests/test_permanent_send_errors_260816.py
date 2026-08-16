"""Night review 260815 (`review/services.md`), findings #1 and #15: a send that fails with
`TelegramBadRequest` must count as a PERMANENT delivery failure, not a transient one.

Both `services/scheduler.py::_safe_send` (#1) and `services/reminders.py::pending_reminder_loop`
(#15) treated only `TelegramForbiddenError` as permanent. "chat not found" / a deleted account
arrives as HTTP 400 -> `TelegramBadRequest` -> fell into the generic `except` -> the give-up was
never recorded -> the same chat_id was retried forever and flooded the log with ERRORs (in
production: 2934 + 2064 identical lines for two ids; 67 for one admin).

Same pathology already fixed on 14.08 in `admin_reply_to_question` (quick `260813-833`) — the
pattern exists in this codebase, so it is copied, not reinvented.

Conventions follow tests/test_audit_fixes_260814.py: plain `def test_*`, `asyncio.run(go())`,
no pytest-asyncio (it is NOT installed in this environment), monkeypatch for module globals.
"""
import asyncio

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from config import config
import database.db as db
import services.scheduler as scheduler


def _forbidden():
    return TelegramForbiddenError(
        method=None, message="Forbidden: bot was blocked by the user"
    )


def _bad_request():
    return TelegramBadRequest(method=None, message="Bad Request: chat not found")


# ── #1: services/scheduler.py::_safe_send ───────────────────────────────────────────────────

def test_safe_send_treats_bad_request_as_permanent_and_runs_hook():
    """"chat not found" (HTTP 400) must record the give-up, exactly like a block does."""
    hook_calls = []

    async def sender(chat_id):
        raise _bad_request()

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def go():
        return await scheduler._safe_send(sender, 777, on_permanent_failure=hook)

    assert asyncio.run(go()) is False
    assert hook_calls == [777], (
        "TelegramBadRequest did not run on_permanent_failure — the chat_id stays a nudge "
        "candidate on every 15-minute scan (review/services.md #1)"
    )


def test_safe_send_bad_request_without_hook_does_not_raise():
    """One-shot broadcast call sites pass no hook; the exception must not escape."""

    async def sender(chat_id):
        raise _bad_request()

    async def go():
        return await scheduler._safe_send(sender, 777)

    assert asyncio.run(go()) is False


def test_safe_send_forbidden_still_permanent_regression():
    """Regression: the pre-existing TelegramForbiddenError behaviour is unchanged."""
    hook_calls = []

    async def sender(chat_id):
        raise _forbidden()

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def go():
        return await scheduler._safe_send(sender, 555, on_permanent_failure=hook)

    assert asyncio.run(go()) is False
    assert hook_calls == [555]


def test_safe_send_transient_error_does_not_run_hook():
    """A network hiccup is TRANSIENT — the recipient must NOT be given up on."""
    hook_calls = []

    async def sender(chat_id):
        raise RuntimeError("network hiccup")

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def go():
        return await scheduler._safe_send(sender, 888, on_permanent_failure=hook)

    assert asyncio.run(go()) is False
    assert hook_calls == [], "a transient failure must never record a permanent give-up"


def test_safe_send_retry_after_still_retries_not_permanent():
    """TelegramRetryAfter must keep taking the retry branch, never the permanent one."""
    attempts = []
    hook_calls = []

    async def sender(chat_id):
        attempts.append(chat_id)
        if len(attempts) == 1:
            raise TelegramRetryAfter(
                method=None, message="Too Many Requests: retry after 2", retry_after=2
            )
        return True

    async def hook(chat_id):
        hook_calls.append(chat_id)

    async def fake_sleep(_seconds):
        return None

    orig_sleep = scheduler.asyncio.sleep
    scheduler.asyncio.sleep = fake_sleep
    try:
        async def go():
            return await scheduler._safe_send(sender, 999, on_permanent_failure=hook)

        assert asyncio.run(go()) is True
    finally:
        scheduler.asyncio.sleep = orig_sleep

    assert len(attempts) == 2, "the 429 retry branch did not run"
    assert hook_calls == [], "TelegramRetryAfter was misclassified as permanent"


def test_scheduler_permanent_send_errors_contains_both_classes():
    """Structural gate: check the tuple object itself, not the source text."""
    assert TelegramForbiddenError in scheduler._PERMANENT_SEND_ERRORS
    assert TelegramBadRequest in scheduler._PERMANENT_SEND_ERRORS


# ── #15: services/reminders.py::pending_reminder_loop ───────────────────────────────────────

class _StopLoop(Exception):
    """Sentinel raised from a patched asyncio.sleep to escape the infinite reminder loop
    after exactly one iteration, without altering services/reminders.py's structure.
    (`await asyncio.sleep(interval)` sits OUTSIDE the inner try, so it is not swallowed.)"""


class _FakeBot:
    """Records deliveries; raises a preset exception for one designated admin id."""

    def __init__(self, failing_id=None, error=None):
        self.delivered = []
        self.failing_id = failing_id
        self.error = error

    async def send_message(self, chat_id, text, *args, **kwargs):
        if self.failing_id is not None and chat_id == self.failing_id:
            raise self.error
        self.delivered.append(chat_id)


def _run_one_reminder_iteration(bot, admin_ids, monkeypatch, tmp_path):
    """One full pending_reminder_loop iteration against a throwaway DB, then stop."""
    config.DB_PATH = str(tmp_path / "test_permanent_send_errors_260816.db")
    asyncio.run(db.init_db())

    import services.reminders as reminders_mod

    monkeypatch.setattr(config, "ADMIN_IDS", admin_ids)

    async def fake_pending_count():
        return 1

    async def fake_sleep(_seconds):
        raise _StopLoop()

    orig_count = reminders_mod.get_pending_count
    orig_sleep = reminders_mod.asyncio.sleep
    reminders_mod.get_pending_count = fake_pending_count
    reminders_mod.asyncio.sleep = fake_sleep
    try:
        try:
            asyncio.run(reminders_mod.pending_reminder_loop(bot))
        except _StopLoop:
            pass
        else:
            raise AssertionError("pending_reminder_loop did not reach the sleep call")
    finally:
        reminders_mod.get_pending_count = orig_count
        reminders_mod.asyncio.sleep = orig_sleep

    return reminders_mod


def test_reminder_bad_request_mutes_admin(tmp_path, monkeypatch):
    """A broken/unreachable admin id must be muted after the FIRST 400, not ERROR forever."""
    import services.reminders as reminders_mod

    reminders_mod._blocked_admins.clear()
    try:
        bot = _FakeBot(failing_id=111, error=_bad_request())
        _run_one_reminder_iteration(bot, [111], monkeypatch, tmp_path)
        assert 111 in reminders_mod._blocked_admins, (
            "TelegramBadRequest did not mute the admin — one identical ERROR every 30 minutes "
            "forever (review/services.md #15)"
        )
    finally:
        reminders_mod._blocked_admins.clear()


def test_reminder_bad_request_does_not_stop_other_admins(tmp_path, monkeypatch):
    """One admin's permanent failure must not abort the fan-out to the rest."""
    import services.reminders as reminders_mod

    reminders_mod._blocked_admins.clear()
    try:
        bot = _FakeBot(failing_id=111, error=_bad_request())
        _run_one_reminder_iteration(bot, [111, 222], monkeypatch, tmp_path)
        assert bot.delivered == [222]
    finally:
        reminders_mod._blocked_admins.clear()


def test_reminder_forbidden_still_mutes_admin_regression(tmp_path, monkeypatch):
    """Regression: the pre-existing block-the-bot muting is unchanged."""
    import services.reminders as reminders_mod

    reminders_mod._blocked_admins.clear()
    try:
        bot = _FakeBot(failing_id=333, error=_forbidden())
        _run_one_reminder_iteration(bot, [333], monkeypatch, tmp_path)
        assert 333 in reminders_mod._blocked_admins
    finally:
        reminders_mod._blocked_admins.clear()


def test_reminder_transient_error_does_not_mute_admin(tmp_path, monkeypatch):
    """A temporary glitch must never silence an admin's reminders for the whole bot run."""
    import services.reminders as reminders_mod

    reminders_mod._blocked_admins.clear()
    try:
        bot = _FakeBot(failing_id=444, error=RuntimeError("network hiccup"))
        _run_one_reminder_iteration(bot, [444], monkeypatch, tmp_path)
        assert 444 not in reminders_mod._blocked_admins
    finally:
        reminders_mod._blocked_admins.clear()


def test_reminders_permanent_send_errors_contains_both_classes():
    """Structural gate: check the tuple object itself, not the source text."""
    import services.reminders as reminders_mod

    assert TelegramForbiddenError in reminders_mod._PERMANENT_SEND_ERRORS
    assert TelegramBadRequest in reminders_mod._PERMANENT_SEND_ERRORS
