"""Хвост делегата, который переживал purge_user: журнал «+N», напоминания, необработанный outbox."""
from __future__ import annotations

import asyncio
import json

import aiosqlite

from config import config
from database import db as bot_db


def _run(coro):
    return asyncio.run(coro)


def test_purge_user_drops_award_remind_and_pending_outbox_for_that_person_only(tmp_path):
    path = str(tmp_path / "forum.db")
    config.DB_PATH = path
    _run(bot_db.init_db())

    async def scenario():
        async with aiosqlite.connect(path) as conn:
            await conn.executemany(
                "INSERT INTO chat_coin_awards "
                "(chat_id, message_id, user_id, amount, awarded_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, 10, 20, 5, 99, "2026-11-09 12:00:00"),
                    (1, 11, 21, 3, 20, "2026-11-09 12:00:00"),
                ],
            )
            await conn.executemany(
                "INSERT INTO session_remind_sends (telegram_id, kind, ref, sent_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (20, "remind", "1", "2026-11-09 12:00:00"),
                    (21, "morning", "msk:2026-11-09", "2026-11-09 12:00:00"),
                ],
            )
            await conn.executemany(
                "INSERT INTO miniapp_outbox (kind, payload, created_at, processed_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    ("submit", json.dumps({"user_id": 20}), "2026-11-09 12:00:00", None),
                    ("submit", json.dumps({"telegram_id": 21}), "2026-11-09 12:00:00", None),
                    (
                        "submit",
                        json.dumps({"user_id": 20}),
                        "2026-11-09 12:00:00",
                        "2026-11-09 12:05:00",
                    ),
                ],
            )
            await conn.commit()

        before = await bot_db.count_user_footprint(20)
        assert before["coins"] == 1
        assert before["queue"] == 2
        removed = await bot_db.purge_user(20)
        assert removed["coins"] == 1
        assert removed["queue"] == 2

        async with aiosqlite.connect(path) as conn:
            async with conn.execute(
                "SELECT user_id, awarded_by FROM chat_coin_awards ORDER BY user_id"
            ) as cursor:
                awards = await cursor.fetchall()
            async with conn.execute(
                "SELECT telegram_id FROM session_remind_sends ORDER BY telegram_id"
            ) as cursor:
                reminds = [row[0] for row in await cursor.fetchall()]
            async with conn.execute(
                "SELECT payload, processed_at FROM miniapp_outbox ORDER BY id"
            ) as cursor:
                outbox = [(json.loads(row[0]), row[1]) for row in await cursor.fetchall()]

        assert awards == [(21, 20)]
        assert reminds == [21]
        assert outbox == [
            ({"telegram_id": 21}, None),
            ({"user_id": 20}, "2026-11-09 12:05:00"),
        ]
        kept = await bot_db.count_user_footprint(21)
        assert kept["coins"] == 1
        assert kept["queue"] == 2
        gone = await bot_db.count_user_footprint(20)
        assert gone["coins"] == 0
        assert gone["queue"] == 0

    _run(scenario())
