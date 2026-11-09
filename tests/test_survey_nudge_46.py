"""Догонялка анкеты: пришли сегодня и не закончили."""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db, survey_db
from services.comms import survey_nudge
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db
from tests.test_surveys_flow_261024 import ADMIN_ID, FakeBot, U1, U2, _add_user, _survey

U3 = 920013


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "nudge.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _arrive(tid: int, day: str | None = None):
    day = day or msk_now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (?, 'entry', ?, 'scan', ?, ?)",
            (tid, now, now, day),
        )
        await conn.commit()


def test_nudge_ids_arrived_unfinished_only(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        await _add_user(U2)
        await _add_user(U3)
        pid, qids = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await db.record_poll_message(pid, U2, None, 2, True)
        await db.record_poll_message(pid, U3, None, 3, True)
        await survey_db.start_survey_progress(pid, U1, qids[0])
        await survey_db.finish_survey(pid, U1, qids[0])
        await _arrive(U1)
        await _arrive(U2)
        return await survey_nudge.nudge_ids(pid)

    assert _run(go()) == [U2]


def test_send_nudge_skips_who_finished_in_between(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, qids = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        ids = await survey_nudge.nudge_ids(pid)
        await survey_db.start_survey_progress(pid, U1, qids[0])
        await survey_db.finish_survey(pid, U1, qids[0])
        bot = FakeBot()
        poll = await db.get_poll(pid)
        return ids, await survey_nudge.send_nudge(bot, poll, ids), bot.messages

    ids, (sent, failed), messages = _run(go())
    assert ids == [U1]
    assert (sent, failed) == (0, 0)
    assert messages == []
