"""Regression: services.background.spawn must hold a strong ref so a fire-and-forget
coroutine is not garbage-collected mid-run (audit systemic finding — WR-02 / MD-01 /
HI-01 / M-01 / MEDIUM-02). Also guards that the ref is released after completion so the
set does not leak."""

import asyncio
import gc

from services.background import spawn, _background_tasks


def test_spawn_survives_dropped_ref_and_gc():
    ran = []

    async def worker():
        # Yield control so the task is suspended — exactly the window where a weakly
        # referenced task would be collectable.
        await asyncio.sleep(0.01)
        ran.append(True)

    async def go():
        spawn(worker())  # return value intentionally discarded (fire-and-forget)
        assert len(_background_tasks) == 1  # strong ref retained while pending
        gc.collect()  # force GC — strong ref must keep the task alive
        # Drain: let the suspended task resume and finish.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)

    asyncio.run(go())

    assert ran == [True]  # task completed despite dropped ref + GC
    assert len(_background_tasks) == 0  # done-callback released the ref (no leak)
