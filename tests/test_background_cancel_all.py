"""services.background.cancel_all: shutdown must cancel every registered fire-and-forget
task and wait for it to unwind — BEFORE main.py closes the bot session, otherwise a task
mid-flight dies on a closed aiohttp ClientSession."""
import asyncio

from services import background
from services.background import _background_tasks, cancel_all, pending_count, spawn


def test_cancel_all_cancels_and_drains_registered_tasks():
    cleaned = []

    async def worker(n):
        try:
            await asyncio.sleep(60)  # would outlive any shutdown without cancellation
        except asyncio.CancelledError:
            cleaned.append(n)
            raise

    async def go():
        t1 = spawn(worker(1))
        t2 = spawn(worker(2))
        await asyncio.sleep(0)  # let them start and suspend
        assert pending_count() == 2
        n = await cancel_all(timeout=5)
        assert n == 2
        assert t1.cancelled() and t2.cancelled()
        assert pending_count() == 0
        assert len(_background_tasks) == 0  # done-callbacks released the refs

    asyncio.run(go())
    assert sorted(cleaned) == [1, 2]


def test_cancel_all_swallows_task_exceptions_and_returns_count():
    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("job failed")

    async def slow():
        await asyncio.sleep(60)

    async def go():
        spawn(boom())
        spawn(slow())
        await asyncio.sleep(0.01)  # boom() finishes (and errors) on its own
        assert pending_count() == 1
        n = await cancel_all(timeout=5)  # must not raise despite the failed task
        assert n == 1
        assert pending_count() == 0

    asyncio.run(go())


def test_cancel_all_noop_when_nothing_running():
    async def go():
        assert await cancel_all() == 0

    asyncio.run(go())


def test_cancel_all_bounded_by_timeout_for_stubborn_task(caplog):
    release = asyncio.Event()

    async def stubborn():
        # Swallows cancellation until released — must not hang shutdown.
        while not release.is_set():
            try:
                await asyncio.wait_for(release.wait(), timeout=60)
            except asyncio.CancelledError:
                continue

    async def go():
        nonlocal release
        release = asyncio.Event()
        t = spawn(stubborn())
        await asyncio.sleep(0)
        n = await cancel_all(timeout=0.05)
        assert n == 1
        assert not t.done()  # abandoned, logged
        release.set()  # let it exit so the loop can close cleanly
        await asyncio.wait([t], timeout=2)
        assert t.done()

    asyncio.run(go())
    assert "did not finish" in caplog.text


def test_main_cancels_background_tasks_before_closing_session():
    """Order in main.py's shutdown path: cancel_all → bot.session.close()."""
    from pathlib import Path

    src = Path(background.__file__).resolve().parent.parent.joinpath("main.py").read_text(encoding="utf-8")
    i_cancel = src.index("await cancel_background_tasks()")
    i_close = src.index("await bot.session.close()")
    assert i_cancel < i_close
