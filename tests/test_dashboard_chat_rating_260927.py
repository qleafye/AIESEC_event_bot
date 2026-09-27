"""Живой рейтинг чата на дашборде (режим «По формуле активности»): dashboard/chat_rating.py.

Фикстура — шаблонная БД бота (fast_init_db), сидинг синхронным sqlite3, чтение — через
dashboard.db.read_conn (mode=ro), как у остальных тестов дашборда.
"""
import sqlite3
from datetime import date, datetime

import pytest

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from tests._dbtpl import fast_init_db

CHAT_ID = -1004444444444
OTHER_CHAT = -1005555555555
NOW = datetime(2026, 9, 24, 15, 0, 0)  # четверг
CHAT = {"chat_id": CHAT_ID, "city": None, "title": "Чат", "label": "Общий чат"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "rating.db")
    config.DB_PATH = path
    fast_init_db()
    return path


def _exec(path, sql, *rows):
    conn = sqlite3.connect(path)
    try:
        for row in rows or [()]:
            conn.execute(sql, row)
        conn.commit()
    finally:
        conn.close()


def _msg(path, mid, author, ts, *, text_len=10, kind="text", reply_mid=None, reply_author=None,
         extra=0, chat_id=CHAT_ID, channel=0):
    _exec(
        path,
        "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len, "
        "reply_to_message_id, reply_to_author_id, is_channel_post, reactions_extra) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, mid, author, ts, kind, text_len, reply_mid, reply_author, channel, extra),
    )


def _react(path, mid, giver, reaction="👍", ts="2026-09-20 10:00:00", chat_id=CHAT_ID):
    _exec(path, "INSERT INTO chat_reactions (chat_id, message_id, telegram_id, reaction, ts) "
                "VALUES (?, ?, ?, ?, ?)", (chat_id, mid, giver, reaction, ts))


def _setting(path, key, value):
    _exec(path, "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _rating(path, period="all", admin_ids=(), chat=CHAT):
    with dash_db.read_conn(path) as conn:
        return chat_rating.chat_rating(conn, chat, period=period, admin_ids=set(admin_ids), now=NOW)


def _by_id(result):
    return {r["telegram_id"]: r for r in result["rows"]}


# ── периоды ──────────────────────────────────────────────────────────────────────────────

def test_period_bounds():
    today = date(2026, 9, 24)  # четверг
    assert chat_rating.period_bounds("all", today) == (None, None)
    assert chat_rating.period_bounds("7d", today) == (date(2026, 9, 18), today)
    assert chat_rating.period_bounds("week", today) == (date(2026, 9, 21), date(2026, 9, 27))
    assert chat_rating.period_bounds("prev_week", today) == (date(2026, 9, 14), date(2026, 9, 20))
    assert chat_rating.period_bounds("что-то", today) == (None, None)
    assert chat_rating.period_bounds(None, today) == (None, None)
    assert [code for code, _label in chat_rating.PERIODS] == ["all", "7d", "week", "prev_week"]


def test_unknown_period_falls_back_to_all(db_path):
    _msg(db_path, 1, 11, "2026-09-01 10:00:00")
    result = _rating(db_path, period="'; DROP TABLE users; --")
    assert result["period"] == "all"
    assert 11 in _by_id(result)


def test_week_period_only_counts_that_week(db_path):
    _msg(db_path, 1, 11, "2026-09-15 10:00:00")   # прошлая неделя
    _msg(db_path, 2, 12, "2026-09-22 10:00:00")   # эта неделя
    assert set(_by_id(_rating(db_path, "prev_week"))) == {11}
    assert set(_by_id(_rating(db_path, "week"))) == {12}
    assert set(_by_id(_rating(db_path, "all"))) == {11, 12}


# ── команда ──────────────────────────────────────────────────────────────────────────────

def test_team_excluded_but_their_replies_and_reactions_count(db_path):
    _exec(db_path, "INSERT INTO staff (telegram_id, role, added_by, added_at) "
                   "VALUES (500, 'reg_manager', 1, '2026-01-01 00:00:00')")
    _exec(db_path, "INSERT INTO chat_admins (chat_id, telegram_id) VALUES (?, 700)", (CHAT_ID,))
    _msg(db_path, 1, 11, "2026-09-20 10:00:00")
    _msg(db_path, 2, 500, "2026-09-20 10:05:00", reply_mid=1, reply_author=11)
    _msg(db_path, 3, 900, "2026-09-20 10:06:00", text_len=500)
    _msg(db_path, 4, 700, "2026-09-20 10:07:00", text_len=300)
    _react(db_path, 1, 700)
    rows = _by_id(_rating(db_path, admin_ids=[900]))
    assert set(rows) == {11}
    assert rows[11]["resonance"] == round(2.0 + 0.5, 1)


def test_group_admin_of_other_chat_is_not_team_here(db_path):
    _exec(db_path, "INSERT INTO chat_admins (chat_id, telegram_id) VALUES (?, 11)", (OTHER_CHAT,))
    _msg(db_path, 1, 11, "2026-09-20 10:00:00")
    assert 11 in _by_id(_rating(db_path))


# ── подписи ──────────────────────────────────────────────────────────────────────────────

def test_display_names_fallback_chain_and_no_full_name(db_path):
    _exec(db_path, "INSERT INTO chat_usernames (telegram_id, username) VALUES (11, 'tg_nick')")
    _exec(db_path, "INSERT INTO users (telegram_id, full_name, username, status) VALUES "
                   "(11, 'Иван Иванов', '@old_nick', 'approved'), "
                   "(12, 'Пётр Петров', '@form_nick', 'approved'), "
                   "(13, 'Анна Сидорова', '-', 'approved')")
    for mid, author in ((1, 11), (2, 12), (3, 13), (4, 14)):
        _msg(db_path, mid, author, "2026-09-20 10:00:00")
    rows = _by_id(_rating(db_path))
    assert rows[11]["display_name"] == "@tg_nick"
    assert rows[12]["display_name"] == "@form_nick"
    assert rows[13]["display_name"] == "13"
    assert rows[14]["display_name"] == "14"
    blob = repr(rows)
    assert "Иван" not in blob and "Пётр" not in blob and "Анна" not in blob


# ── реакции ──────────────────────────────────────────────────────────────────────────────

def test_reactions_extra_adds_to_received(db_path):
    _msg(db_path, 1, 11, "2026-09-20 10:00:00", extra=3)
    _react(db_path, 1, 12)
    with dash_db.read_conn(db_path) as conn:
        records = chat_rating.load_records(conn, CHAT_ID)
    assert records[0].reactions_received == 4
    assert records[0].reaction_giver_ids == (12,)
    rows = _by_id(_rating(db_path))
    assert rows[11]["resonance"] == 2.0  # 4 × 0.5
    assert rows[12]["giving"] == round(0.25, 1)


def test_media_and_sticker_kinds_map_to_record_flags(db_path):
    _msg(db_path, 1, 11, "2026-09-20 10:00:00", kind="media", text_len=0)
    _msg(db_path, 2, 11, "2026-09-20 12:00:00", kind="sticker", text_len=0)
    _msg(db_path, 3, 11, "2026-09-20 14:00:00", kind="other", text_len=0)
    with dash_db.read_conn(db_path) as conn:
        recs = {r.message_id: r for r in chat_rating.load_records(conn, CHAT_ID)}
    assert recs[1].has_media and not recs[1].is_sticker
    assert recs[2].has_media and recs[2].is_sticker
    assert not recs[3].has_media and not recs[3].is_sticker


def test_channel_posts_are_not_authors(db_path):
    _msg(db_path, 1, -1009999, "2026-09-20 10:00:00", channel=1, text_len=400)
    _msg(db_path, 2, 11, "2026-09-20 10:05:00", reply_mid=1)
    rows = _by_id(_rating(db_path))
    assert set(rows) == {11}


# ── веса из настроек ─────────────────────────────────────────────────────────────────────

def test_weights_from_bot_settings_and_garbage_falls_back(db_path):
    _msg(db_path, 1, 11, "2026-09-20 10:00:00")
    _msg(db_path, 2, 12, "2026-09-20 10:05:00", reply_mid=1, reply_author=11)
    assert _by_id(_rating(db_path))[11]["resonance"] == 2.0
    _setting(db_path, "chat_rating_reply_weight", "3")
    result = _rating(db_path)
    assert _by_id(result)[11]["resonance"] == 3.0
    assert "3×ответы" in result["formula_text"]
    _setting(db_path, "chat_rating_reply_weight", "много")
    assert _by_id(_rating(db_path))[11]["resonance"] == 2.0


def test_retention_days_from_settings(db_path):
    assert _rating(db_path)["retention_days"] == 180
    _setting(db_path, "chat_rating_retention_days", "90")
    assert _rating(db_path)["retention_days"] == 90
    _setting(db_path, "chat_rating_retention_days", "мусор")
    assert _rating(db_path)["retention_days"] == 180


# ── бот-админ ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [
    ("administrator", True), ("creator", True), ("member", False), ("left", False),
])
def test_bot_admin_flag(db_path, status, expected):
    _exec(db_path, "INSERT INTO chat_bot_state (chat_id, bot_status) VALUES (?, ?)",
          (CHAT_ID, status))
    assert _rating(db_path)["bot_admin"] is expected


def test_bot_admin_unknown_without_row(db_path):
    assert _rating(db_path)["bot_admin"] is None


# ── пустой чат, порядок, форма строки ────────────────────────────────────────────────────

def test_empty_chat_gives_empty_rows(db_path):
    result = _rating(db_path)
    assert result["rows"] == []
    assert result["periods"] and result["formula_text"].startswith("Формула балла")


def test_rows_sorted_by_score_with_places_and_all_parts(db_path):
    _msg(db_path, 1, 11, "2026-09-20 10:00:00", text_len=10)
    _msg(db_path, 2, 12, "2026-09-20 10:00:00", text_len=900)
    _msg(db_path, 3, 12, "2026-09-21 10:00:00", text_len=900)
    rows = _rating(db_path)["rows"]
    assert [r["telegram_id"] for r in rows] == [12, 11]
    assert [r["place"] for r in rows] == [1, 2]
    for key in ("display_name", "score", "volume", "resonance", "regularity", "giving",
                "messages", "active_days"):
        assert key in rows[0]
    assert rows[0]["messages"] == 2 and rows[0]["active_days"] == 2


def test_rows_capped_at_50(db_path):
    conn = sqlite3.connect(db_path)
    for i in range(60):
        conn.execute(
            "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len) "
            "VALUES (?, ?, ?, '2026-09-20 10:00:00', 'text', ?)", (CHAT_ID, i + 1, 1000 + i, i),
        )
    conn.commit()
    conn.close()
    assert len(_rating(db_path)["rows"]) == 50


def test_other_chat_messages_not_counted(db_path):
    _msg(db_path, 1, 11, "2026-09-20 10:00:00", chat_id=OTHER_CHAT)
    assert _rating(db_path)["rows"] == []
