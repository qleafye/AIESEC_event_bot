"""Живой учёт чата для рейтинга: журнал сообщений БЕЗ ТЕКСТА, текущие реакции, ники,
админы группы и срок хранения.

Владелец 20.09: «для отклика и реакций вживую нужна таблица message_id -> автор без текста».
Команда = staff + ADMIN_IDS + админы группы. pytest-asyncio нет — `asyncio.run()`; БД —
`fast_init_db`.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from tests._dbtpl import fast_init_db

ADMIN = 900927501
CHAT = -1009275001
A, B, C = 900927511, 900927512, 900927513


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "capture.db")
    config.ADMIN_IDS = [ADMIN]
    fast_init_db()


async def _rows(sql, *params):
    async with db._connect() as conn:
        async with conn.execute(sql, params) as cur:
            return await cur.fetchall()


def _log(mid, author, ts="2026-09-27 10:00:00", **kw):
    params = dict(kind="text", text_len=10, reply_to_message_id=None, reply_to_author_id=None,
                  is_channel_post=False)
    params.update(kw)
    return db.log_chat_message(CHAT, mid, author, ts, **params)


# ── Задача 1: схема и хелперы ─────────────────────────────────────────────────────────────

def test_tables_exist_and_init_is_idempotent(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A))
    _run(db.init_db())
    names = {r[0] for r in _run(_rows("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"chat_messages", "chat_reactions", "chat_usernames", "chat_admins"} <= names
    assert _run(_rows("SELECT COUNT(*) FROM chat_messages"))[0][0] == 1
    cols = {r[1] for r in _run(_rows("PRAGMA table_info(chat_messages)"))}
    assert not [c for c in cols if "text" in c and c != "text_len"]


def test_log_chat_message_ignores_duplicates(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, text_len=10))
    _run(_log(1, A, text_len=99))
    rows = _run(_rows("SELECT text_len, kind, source FROM chat_messages"))
    assert rows == [(10, "text", "live")]


def test_update_len_only_for_existing_row(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, text_len=10))
    _run(db.update_chat_message_len(CHAT, 1, 42))
    _run(db.update_chat_message_len(CHAT, 2, 42))
    assert _run(_rows("SELECT message_id, text_len FROM chat_messages")) == [(1, 42)]


def test_set_chat_reactions_mirrors_current_state(tmp_path):
    _ready(tmp_path)
    ts = "2026-09-27 10:05:00"
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍", "🔥"], ts))
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍"], ts))
    assert _run(_rows("SELECT reaction FROM chat_reactions")) == [("👍",)]
    _run(db.set_chat_reactions(CHAT, 1, B, [], ts))
    assert _run(_rows("SELECT COUNT(*) FROM chat_reactions"))[0][0] == 0


def test_upsert_chat_username(tmp_path):
    _ready(tmp_path)
    _run(db.upsert_chat_username(A, "@alice"))
    _run(db.upsert_chat_username(A, None))
    _run(db.upsert_chat_username(A, ""))
    assert _run(_rows("SELECT username FROM chat_usernames WHERE telegram_id = ?", A)) == [("alice",)]
    _run(db.upsert_chat_username(A, "alice2"))
    assert _run(_rows("SELECT username FROM chat_usernames WHERE telegram_id = ?", A)) == [("alice2",)]


def test_replace_chat_admins(tmp_path):
    _ready(tmp_path)
    _run(db.replace_chat_admins(CHAT, [A, B]))
    _run(db.replace_chat_admins(CHAT, [B]))
    assert _run(_rows("SELECT telegram_id FROM chat_admins WHERE chat_id = ?", CHAT)) == [(B,)]


def test_prune_chat_history(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, ts="2026-01-01 10:00:00"))
    _run(_log(2, A, ts="2026-09-27 10:00:00"))
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍"], "2026-09-27 10:00:00"))  # сообщения уже нет
    _run(db.set_chat_reactions(CHAT, 2, B, ["👍"], "2026-01-02 10:00:00"))  # старая реакция
    _run(db.set_chat_reactions(CHAT, 2, C, ["🔥"], "2026-09-27 11:00:00"))
    counts = _run(db.prune_chat_history("2026-06-01 00:00:00"))
    assert counts["messages"] == 1
    assert _run(_rows("SELECT message_id FROM chat_messages")) == [(2,)]
    assert _run(_rows("SELECT telegram_id FROM chat_reactions")) == [(C,)]


def test_purge_user_clears_all_chat_traces(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A))
    _run(_log(2, B, reply_to_message_id=1, reply_to_author_id=A))
    _run(db.set_chat_reactions(CHAT, 2, A, ["👍"], "2026-09-27 10:00:00"))
    _run(db.upsert_chat_username(A, "alice"))
    _run(db.replace_chat_admins(CHAT, [A, C]))
    _run(db.purge_user(A))
    assert _run(_rows("SELECT telegram_id, reply_to_author_id FROM chat_messages")) == [(B, None)]
    assert _run(_rows("SELECT COUNT(*) FROM chat_reactions"))[0][0] == 0
    assert _run(_rows("SELECT COUNT(*) FROM chat_usernames"))[0][0] == 0
    assert _run(_rows("SELECT telegram_id FROM chat_admins")) == [(C,)]


def test_purge_chat_data_clears_per_chat_tables(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A))
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍"], "2026-09-27 10:00:00"))
    _run(db.replace_chat_admins(CHAT, [A]))
    _run(db.upsert_chat_username(A, "alice"))
    _run(db.purge_chat_data(CHAT))
    for table in ("chat_messages", "chat_reactions", "chat_admins"):
        assert _run(_rows(f"SELECT COUNT(*) FROM {table}"))[0][0] == 0, table
    assert _run(_rows("SELECT COUNT(*) FROM chat_usernames"))[0][0] == 1  # ники — не по чату
