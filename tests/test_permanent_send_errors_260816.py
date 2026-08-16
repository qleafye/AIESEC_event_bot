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
