"""Живой рейтинг чата: паритет «экспорт Telegram Desktop» vs «живые таблицы + дашборд».

Один и тот же разговор описан данными (CONVERSATION) и отрисован двумя способами:
- как result.json экспорта (корень топика — сервисное сообщение, реакции — count + recent);
- как строки chat_messages / chat_reactions, какими их записал бы живой учёт в группе
  (ответ на корень топика — без reply-полей, неизвестные дарители — reactions_extra).
Балл каждого человека по обоим путям обязан совпасть: формула одна (chat_score), различаются
только адаптеры.
"""
import sqlite3
from datetime import date

import pytest

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from tests._dbtpl import fast_init_db
from tools import chat_export_stats as ces

from core import chat_score

CHAT_ID = -1003333333333
ROOT_ID = 1          # корень топика (topic_created)
STAFF_ID = 500       # таблица staff
ADMIN_ID = 900       # ADMIN_IDS
GROUP_ADMIN_ID = 700  # админ группы (chat_admins)
TEAM = {STAFF_ID, ADMIN_ID, GROUP_ADMIN_ID}

# (id, author, "YYYY-MM-DD HH:MM:SS", kind, text_len, reply_to, forwarded, reactions)
# kind: text | photo | sticker | animation; reactions: [(emoji, [givers], hidden_count)]
CONVERSATION = [
    (10, 1, "2026-09-01 10:00:00", "text", 40, ROOT_ID, False, []),
    (11, 1, "2026-09-01 10:01:00", "text", 300, ROOT_ID, False, []),        # склейка < 2 мин
    (12, 2, "2026-09-01 10:05:00", "text", 60, 11, False, [("👍", [1], 0)]),
    (13, STAFF_ID, "2026-09-01 10:06:00", "text", 200, 12, False, []),        # команда -> делегату
    (14, 3, "2026-09-01 10:10:00", "text", 20, 13, False, []),                # делегат -> команде
    (15, 3, "2026-09-01 10:20:00", "text", 15, 14, False, []),                # ответ самому себе
    (16, 4, "2026-09-01 11:00:00", "sticker", 0, None, False, []),
    (17, 4, "2026-09-01 11:05:00", "photo", 90, None, False, []),             # фото с подписью
    (18, 2, "2026-09-01 12:00:00", "text", 500, None, True, []),              # пересланное
    (19, 1, "2026-09-01 13:00:00", "text", 100, None, False, [
        ("❤", [2, 3, 4, STAFF_ID, ADMIN_ID], 2),
        ("🔥", [GROUP_ADMIN_ID, 2, 3], 2),
    ]),                                                                       # 12 реакций
    (20, 2, "2026-09-02 09:00:00", "text", 30, 19, False, []),
    (21, 3, "2026-09-02 09:01:00", "text", 30, 19, False, []),
    (22, 4, "2026-09-02 09:02:00", "text", 30, 19, False, []),
    (23, STAFF_ID, "2026-09-02 09:03:00", "text", 30, 19, False, []),
    (24, ADMIN_ID, "2026-09-02 09:04:00", "text", 30, 19, False, []),
    (25, GROUP_ADMIN_ID, "2026-09-02 09:05:00", "text", 30, 19, False, []),   # 6 ответов на 19
    (26, 5, "2026-09-02 10:00:00", "text", 50, None, False, []),
    (27, STAFF_ID, "2026-09-02 10:30:00", "text", 80, 26, False, [("👍", [5], 0)]),
    (28, 1, "2026-09-02 18:00:00", "animation", 0, 26, False, []),
    (29, ADMIN_ID, "2026-09-02 19:00:00", "text", 800, ROOT_ID, False, [("👏", [1, 2], 0)]),
]


def _export_message(mid, author, ts, kind, text_len, reply_to, forwarded, reactions):
    msg = {
        "id": mid, "type": "message", "date": ts.replace(" ", "T"),
        "from": f"Человек {author}", "from_id": f"user{author}",
        "text": "x" * text_len,
    }
    if reply_to is not None:
        msg["reply_to_message_id"] = reply_to
    if forwarded:
        msg["forwarded_from"] = "Кто-то"
    if kind == "photo":
        msg["photo"] = "photos/p.jpg"
    elif kind in ("sticker", "animation"):
        msg["media_type"] = kind
        msg["file"] = "stickers/s.webp"
    if reactions:
        msg["reactions"] = [
            {"type": "emoji", "emoji": emoji, "count": len(givers) + hidden,
             "recent": [{"from_id": f"user{g}", "date": ts} for g in givers]}
            for emoji, givers, hidden in reactions
        ]
    return msg


def export_dict(conversation=CONVERSATION) -> dict:
    messages = [{"id": ROOT_ID, "type": "service", "date": "2026-09-01T09:00:00",
                 "action": "topic_created", "title": "Общение"}]
    messages += [_export_message(*row) for row in conversation]
    return {"name": "Чат делегатов", "type": "private_supergroup", "id": 3333333333,
            "messages": messages}


def _live_kind(kind: str) -> str:
    return {"text": "text", "photo": "media", "sticker": "sticker", "animation": "sticker"}[kind]


def insert_live(db_path: str, conversation=CONVERSATION, chat_id: int = CHAT_ID) -> None:
    """Те же сообщения — как их записал бы живой учёт (handlers/group_chat.py)."""
    authors = {row[0]: row[1] for row in conversation}
    conn = sqlite3.connect(db_path)
    try:
        for mid, author, ts, kind, text_len, reply_to, forwarded, reactions in conversation:
            reply_mid = reply_author = None
            if reply_to is not None and reply_to != ROOT_ID:
                reply_mid, reply_author = reply_to, authors.get(reply_to)
            hidden_total = sum(h for _e, _g, h in reactions)
            conn.execute(
                "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len, "
                "reply_to_message_id, reply_to_author_id, is_channel_post, reactions_extra, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'live')",
                (chat_id, mid, author, ts, _live_kind(kind), 0 if forwarded else text_len,
                 reply_mid, reply_author, hidden_total),
            )
            for emoji, givers, _hidden in reactions:
                for g in givers:
                    conn.execute(
                        "INSERT INTO chat_reactions (chat_id, message_id, telegram_id, reaction, ts) "
                        "VALUES (?, ?, ?, ?, ?)", (chat_id, mid, g, emoji, ts),
                    )
        conn.commit()
    finally:
        conn.close()


def export_scores(data: dict, *, since=None, until=None) -> dict:
    messages, reply_index, _present = ces.parse_messages(data["messages"])
    aggs = ces.aggregate(messages, reply_index, since=since, until=until)
    return aggs, ces.score_authors(aggs)


def live_scores(db_path: str, *, since=None, until=None, chat_id: int = CHAT_ID):
    with dash_db.read_conn(db_path) as conn:
        records = chat_rating.load_records(conn, chat_id, until=until)
    aggs = chat_score.aggregate_records(records, since=since, until=until)
    return aggs, chat_score.score_authors(aggs)


def _rounded(scores: dict, keep) -> dict:
    return {
        pid: {k: round(v, 6) for k, v in sc.items()}
        for pid, sc in scores.items() if keep(pid)
    }


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "parity.db")
    config.DB_PATH = path
    fast_init_db()
    insert_live(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO staff (telegram_id, role, added_by, added_at) VALUES (?, 'reg_manager', 1, "
        "'2026-01-01 00:00:00')", (STAFF_ID,),
    )
    conn.execute("INSERT INTO chat_admins (chat_id, telegram_id) VALUES (?, ?)",
                 (CHAT_ID, GROUP_ADMIN_ID))
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize("since,until", [
    (None, None),
    (date(2026, 9, 2), None),
    (None, date(2026, 9, 1)),
])
def test_export_and_live_paths_give_identical_scores(db_path, since, until):
    exp_aggs, exp = export_scores(export_dict(), since=since, until=until)
    live_aggs, live = live_scores(db_path, since=since, until=until)

    def keep(pid):
        return pid not in TEAM

    assert _rounded(exp, keep) == _rounded(live, keep)
    assert _rounded(exp, keep)  # фикстура не пустая
    for pid in exp_aggs:
        if keep(pid):
            assert exp_aggs[pid].messages == live_aggs[pid].messages
            assert exp_aggs[pid].active_days == live_aggs[pid].active_days
            assert exp_aggs[pid].reactions_received == live_aggs[pid].reactions_received


def test_parity_fixture_exercises_the_tricky_parts(db_path):
    _aggs, exp = export_scores(export_dict())
    # 6 ответов на сообщение 19 -> засчитано не больше 5 (вес 2) + 12 реакций -> не больше 10
    agg = _aggs[1]
    assert sorted(agg.reply_counts) == [1, 6]
    assert 12 in agg.reaction_counts
    # делегату 5 ответила только команда — резонанс всё равно есть
    assert exp[5]["resonance"] > 0


def test_chat_rating_rows_match_export_tool_minus_team(db_path):
    exp_aggs, exp = export_scores(export_dict())
    with dash_db.read_conn(db_path) as conn:
        result = chat_rating.chat_rating(
            conn, {"chat_id": CHAT_ID, "city": None}, period="all",
            admin_ids={ADMIN_ID}, now=chat_rating_now(), registered_only=False,
        )
    rows = result["rows"]
    ids_in_rows = {r["telegram_id"] for r in rows}
    assert ids_in_rows == {pid for pid in exp if pid not in TEAM}
    for r in rows:
        assert r["score"] == round(exp[r["telegram_id"]]["score"], 1)


def chat_rating_now():
    from datetime import datetime
    return datetime(2026, 9, 3, 12, 0, 0)


def test_team_ids_is_staff_plus_admin_ids_plus_group_admins(db_path):
    with dash_db.read_conn(db_path) as conn:
        assert chat_rating.team_ids(conn, CHAT_ID, {ADMIN_ID}) == TEAM
        # админы ДРУГОЙ группы командой этого чата не считаются
        assert GROUP_ADMIN_ID not in chat_rating.team_ids(conn, -1, set())
        result = chat_rating.chat_rating(
            conn, {"chat_id": CHAT_ID, "city": None}, period="all",
            admin_ids={ADMIN_ID}, now=chat_rating_now(), registered_only=False,
        )
    ids_in_rows = {r["telegram_id"] for r in result["rows"]}
    assert not ids_in_rows & TEAM
    assert ids_in_rows == {1, 2, 3, 4, 5}
