"""Shared fire-and-forget task helper + shutdown hook.

WR-02 / audit systemic finding: the event loop keeps only *weak* references to tasks,
so a bare ``asyncio.create_task()`` can be garbage-collected mid-run — silently dropping
a suspended background job (Sheets export, welcome drain, album broadcast, reminder loop).

``spawn()`` holds a strong reference in a module-level set until the task finishes, then
discards it in a done-callback. Lives in ``services`` (not ``main.py``) so that both the
startup wiring in ``main.py`` and the request handlers can import it without the circular
dependency ``handlers -> main`` would create.

``cancel_all()`` is the other half — lifecycle. On shutdown ``main.py`` used to close the
bot session while these tasks were still running; a task mid-flight then hit a closed
aiohttp session (or got cut between "DB updated" and "Sheets appended"). ``cancel_all()``
cancels every registered task and waits for them to unwind, and must run BEFORE
``bot.session.close()``.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Schedule *coro* fire-and-forget while holding a strong ref until it completes."""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


def pending_count() -> int:
    """Number of registered tasks that have not finished yet."""
    return sum(1 for t in _background_tasks if not t.done())


async def cancel_all(timeout: float = 10.0) -> int:
    """Cancel every still-running background task and wait (bounded) for them to finish.

    Returns the number of tasks that were cancelled. CancelledError and any other exception
    raised while a task unwinds are swallowed (``return_exceptions=True``) — shutdown must
    never fail because a background job failed. A task that ignores cancellation for longer
    than *timeout* is logged and abandoned rather than blocking the shutdown.
    """
    tasks = [t for t in list(_background_tasks) if not t.done()]
    if not tasks:
        return 0
    for t in tasks:
        t.cancel()
    try:
        # asyncio.wait (not wait_for+gather): on timeout it simply returns the still-pending
        # set, whereas wait_for would cancel the gather and then block until every child
        # finished — a task that swallows cancellation would hang the shutdown forever.
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in done:  # retrieve results so no "exception was never retrieved" noise
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.debug("background: task ended with %r during shutdown", exc)
        if pending:
            logger.warning(
                "background: %d task(s) did not finish within %.1fs after cancel — abandoning",
                len(pending), timeout,
            )
    except Exception:  # noqa: BLE001 — shutdown path, never raise
        logger.warning("background: error while awaiting cancelled tasks", exc_info=True)
    return len(tasks)
