"""DebouncedJob: «последний вызов побеждает», алерт один раз на серию сбоев."""
from __future__ import annotations

import asyncio

import pytest

from services.infra.debounce import DebouncedJob


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def fake_alert(text):
        sent.append(text)

    monkeypatch.setattr("services.sheets.sheets._send_admin_alert", fake_alert)
    return sent


def _job():
    return DebouncedJob("t", "авария", delay=0.01)


def test_burst_runs_once(alerts):
    async def go():
        job = _job()
        calls = []

        async def work():
            calls.append(1)
            return (1, 1)

        job.set_job(work)
        for _ in range(5):
            job.request()
        await asyncio.sleep(0.1)
        return calls

    assert asyncio.run(go()) == [1]
    assert alerts == []


def test_no_job_is_silent(alerts):
    async def go():
        job = _job()
        job.request()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert alerts == []


def test_failed_tuple_alerts_once_and_resets(alerts):
    async def go():
        job = _job()
        results = [(-1, 3), (3, -1), (2, 2), (-1, -1)]

        async def work():
            return results.pop(0)

        job.set_job(work)
        for _ in range(4):
            job.request()
            await asyncio.sleep(0.05)

    asyncio.run(go())
    # серия из двух сбоев — один алерт; после успеха новая серия — ещё один
    assert alerts == ["авария", "авария"]


def test_exception_alerts_once(alerts):
    async def go():
        job = _job()

        async def work():
            raise RuntimeError("boom")

        job.set_job(work)
        for _ in range(2):
            job.request()
            await asyncio.sleep(0.05)

    asyncio.run(go())
    assert alerts == ["авария"]
