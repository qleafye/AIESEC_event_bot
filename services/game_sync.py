"""Phase 09.1 (D, GAME-07): debounced background resync of the gamification sheet tabs
(«Гейма» matrix + «История сдач», plus the per-city pairs when the cities module is on —
services/game_sheets.py).

CONTEXT.md D (locked): a manager should not have to remember the "🔄 Таблица геймы" button
for the tabs to stay fresh. Every significant event (task created, moderator decision,
coin edit) requests a resync; 20 events in a row must still produce exactly ONE rebuild —
"last call wins": each `request_resync()` cancels whatever is still waiting and restarts a
fresh ~30s timer. This is a plain asyncio task, not a persistent third-party job-store
scheduler (CONTEXT.md D is explicit about this) — the debounce only needs "wait, then
maybe-cancel", which `asyncio.sleep` + `Task.cancel()` already do on their own.

Import discipline: this module imports ONLY stdlib + `services.background.spawn` +
`services.sheets._send_admin_alert`. It never reaches into the admin request layer above
`services/` — that layer already imports `services.*`, so an import the other way would be
a cycle. Instead, the admin layer hands its own `rebuild_game_sheets` coroutine down via
`set_rebuild()` at import time (inversion of control — the same shape `services/sheets.py`
uses for `set_alert_bot`).
"""

import asyncio
import logging

from services.background import spawn
from services.sheets import _send_admin_alert

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 30

# Registered by the admin request layer at module import time via set_rebuild(). Left unset
# here so this module can be imported standalone (e.g. by tests) without pulling in the rest
# of that (heavy) module.
_rebuild = None
_pending: "asyncio.Task | None" = None
# One-shot admin warning per failure streak — same shape as services.sheets._alert_bot_warned:
# set on the first failure, cleared on the next success, so a run of failures never spams.
_failure_warned = False


def set_rebuild(fn) -> None:
    """Register the coroutine function request_resync() should eventually call. Called once
    by the admin request layer right after it defines rebuild_game_sheets()."""
    global _rebuild
    _rebuild = fn


def request_resync(delay: float | None = None) -> None:
    """Schedule (or reschedule) a debounced rebuild. Synchronous — does not await anything,
    safe to call from any handler right after its own state change. "Last call wins": if a
    timer is already pending (scheduled but not yet finished running), it is cancelled and
    replaced by a fresh one, so a burst of calls collapses into a single rebuild ~`delay`
    seconds after the LAST call."""
    global _pending
    if _pending is not None and not _pending.done():
        _pending.cancel()
    _pending = spawn(_run_after(delay if delay is not None else DEBOUNCE_SECONDS))


async def _run_after(delay: float) -> None:
    global _failure_warned
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # Superseded by a later request_resync() -- this timer never runs the rebuild, and
        # the cancellation stops right here (fire-and-forget task, nothing awaits us).
        return

    if _rebuild is None:
        # Early-boot edge case: something requested a resync before the admin request layer
        # ran its module-level set_rebuild(rebuild_game_sheets) call. Not an error -- skip.
        logger.warning("game_sync: request_resync fired before set_rebuild() was called — skipping")
        return

    try:
        result = await _rebuild()
    except Exception:
        logger.exception("game_sync: background rebuild of the gamification sheet tabs failed")
        if not _failure_warned:
            await _send_admin_alert(
                "⚠️ Автосинк вкладок геймы: фоновая пересборка упала с ошибкой (см. лог сервера)."
            )
            _failure_warned = True
        return

    # Quick GAME-CITY-TABS: rebuild_game_sheets returns one row count per tab (2 with the
    # cities module off, 2 + 2 per enabled city with it on) -- any -1 means a failed tab.
    ok = True
    if isinstance(result, tuple) and result:
        ok = all(written >= 0 for written in result)

    if not ok:
        if not _failure_warned:
            await _send_admin_alert(
                "⚠️ Автосинк вкладок геймы: Google Sheets вернул ошибку при пересборке "
                "(см. лог сервера)."
            )
            _failure_warned = True
    else:
        _failure_warned = False


def reset_for_tests() -> None:
    """Reset module-level state between tests -- there is no fixture in this project that
    resets module state automatically, and this module intentionally has none (single
    in-process debounce, not per-request)."""
    global _pending, _failure_warned
    _pending = None
    _failure_warned = False
