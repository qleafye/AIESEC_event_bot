"""Срок хранения истории чата распространяется и на ники (chat_usernames).

Подсказка настройки обещает «записи старше этого срока удаляются», а ники авторов оставались
навсегда. Теперь суточная чистка снимает ник вместе с последним следом человека в журнале:
нет ни сообщений, ни реакций — нет и ника. pytest-asyncio нет — `asyncio.run()`.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from tests._dbtpl import fast_init_db

CHAT = -1009280001
A, B, C, D = 900928011, 900928012, 900928013, 900928014


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "retention.db")
    config.ADMIN_IDS = [1]
    fast_init_db()


async def _rows(sql, *params):
    async with db._connect() as conn:
        async with conn.execute(sql, params) as cur:
            return await cur.fetchall()


def _log(mid, author, ts):
    return db.log_chat_message(CHAT, mid, author, ts, kind="text", text_len=5,
                               reply_to_message_id=None, reply_to_author_id=None)


def test_prune_drops_usernames_without_chat_traces(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, "2026-09-27 10:00:00"))          # свежее сообщение — ник остаётся
    _run(_log(2, B, "2026-09-27 10:00:00"))
    _run(_log(3, D, "2026-01-01 10:00:00"))          # всё старое — уйдёт вместе с ником
    _run(db.set_chat_reactions(CHAT, 2, C, ["👍"], "2026-09-27 11:00:00"))  # только реакция
    for uid, nick in ((A, "alice"), (B, "bob"), (C, "carol"), (D, "dave"), (900928099, "ghost")):
        _run(db.upsert_chat_username(uid, nick))

    counts = _run(db.prune_chat_history("2026-06-01 00:00:00"))

    left = {r[0] for r in _run(_rows("SELECT telegram_id FROM chat_usernames"))}
    assert left == {A, B, C}
    assert counts["usernames"] == 2
