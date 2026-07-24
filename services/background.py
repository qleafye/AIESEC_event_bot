"""Shared fire-and-forget task helper.

WR-02 / audit systemic finding: the event loop keeps only *weak* references to tasks,
so a bare ``asyncio.create_task()`` can be garbage-collected mid-run — silently dropping
a suspended background job (Sheets export, welcome drain, album broadcast, reminder loop).

``spawn()`` holds a strong reference in a module-level set until the task finishes, then
discards it in a done-callback. Lives in ``services`` (not ``main.py``) so that both the
startup wiring in ``main.py`` and the request handlers can import it without the circular
dependency ``handlers -> main`` would create.
"""

import asyncio

_background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Schedule *coro* fire-and-forget while holding a strong ref until it completes."""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t
