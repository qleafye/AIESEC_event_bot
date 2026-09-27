"""Рейтинг чата «По правилам города» на дашборде (пресет СПб «коины»): dashboard/chat_rating.py.

Расчёт только для показа: ничего не пишет и не шлёт. Посты — любые сообщения команды
(порог длины по умолчанию 0), комментарий — настоящий ответ на пост.
"""
import sqlite3
from datetime import datetime

import pytest

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from tests._dbtpl import fast_init_db

SPB_CHAT = -1006666666666
MSK_CHAT = -1007777777777
NOW = datetime(2026, 9, 24, 15, 0, 0)  # четверг; прошлая неделя 14.09–20.09
SPB = {"chat_id": SPB_CHAT, "city": "spb", "title": "СПб", "label": "Санкт-Петербург"}
MSK = {"chat_id": MSK_CHAT, "city": "msk", "title": "Мск", "label": "Москва"}
STAFF = 500
OWNER = 900  # ADMIN_IDS


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "rules.db")
    config.DB_PATH = path
    fast_init_db()
    _exec(path, "INSERT INTO staff (telegram_id, role, added_by, added_at) "
                "VALUES (?, 'reg_manager', 1, '2026-01-01 00:00:00')", (STAFF,))
    _setting(path, "chat_rating_mode__city__spb", "rules")
    return path


def _exec(path, sql, *rows):
    conn = sqlite3.connect(path)
    try:
        for row in rows or [()]:
            conn.execute(sql, row)
        conn.commit()
    finally:
        conn.close()


def _setting(path, key, value):
    _exec(path, "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _msg(path, mid, author, ts, *, text_len=10, reply_mid=None, reply_author=None,
         chat_id=SPB_CHAT, channel=0):
    _exec(
        path,
        "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len, "
        "reply_to_message_id, reply_to_author_id, is_channel_post) VALUES (?, ?, ?, ?, 'text', ?, ?, ?, ?)",
        (chat_id, mid, author, ts, text_len, reply_mid, reply_author, channel),
    )


def _member(path, tid, chat_id=SPB_CHAT, status="member"):
    _exec(path, "INSERT INTO chat_members (chat_id, telegram_id, status, source) "
                "VALUES (?, ?, ?, 'test')", (chat_id, tid, status))


def _user(path, tid, *, referrer=None, status="approved", approved_at="2026-09-16 10:00:00",
          season="YL 26/2", username="-"):
    _exec(path, "INSERT INTO users (telegram_id, full_name, username, status, referrer_id, "
                "approved_at, season) VALUES (?, ?, ?, ?, ?, ?, ?)",
          (tid, f"ФИО {tid}", username, status, referrer, approved_at, season))


def _submission(path, task_id, user_id, status="approved", reviewed_at="2026-09-16 10:00:00"):
    _exec(path, "INSERT INTO game_submissions (task_id, user_id, content_type, content, "
                "submitted_at, status, reviewed_at) VALUES (?, ?, 'text', 'x', ?, ?, ?)",
          (task_id, user_id, reviewed_at, status, reviewed_at))


def _checkin(path, tid, point, day):
    _exec(path, "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
                "VALUES (?, ?, ?, 'test', ?, ?)",
          (tid, point, f"{day} 10:00:00", f"{day} 10:00:00", day))


def _rules(path, period="all", chat=SPB):
    with dash_db.read_conn(path) as conn:
        return chat_rating.rules_rating(conn, chat, period=period, admin_ids={OWNER}, now=NOW)


def _rows(result):
    return {r["telegram_id"]: r for r in result["rows"]}


# ── режим по городу ──────────────────────────────────────────────────────────────────────

def test_chat_mode_per_city_with_global_fallback(db_path):
    with dash_db.read_conn(db_path) as conn:
        assert chat_rating.chat_mode(conn, SPB) == "rules"
        assert chat_rating.chat_mode(conn, MSK) == "formula"
        assert chat_rating.chat_mode(conn, {"chat_id": 1, "city": None}) == "formula"
    _setting(db_path, "chat_rating_mode__city__spb", "*")
    _setting(db_path, "chat_rating_mode", "rules")
    with dash_db.read_conn(db_path) as conn:
        assert chat_rating.chat_mode(conn, SPB) == "rules"
        assert chat_rating.chat_mode(conn, MSK) == "rules"
    _setting(db_path, "chat_rating_mode", "ерунда")
    with dash_db.read_conn(db_path) as conn:
        assert chat_rating.chat_mode(conn, MSK) == "formula"


# ── комментарии ──────────────────────────────────────────────────────────────────────────

def test_comment_under_team_message_10_and_valuable_20_one_award_per_post(db_path):
    _msg(db_path, 1, STAFF, "2026-09-15 10:00:00", text_len=5)   # любое сообщение команды — пост
    _msg(db_path, 2, 11, "2026-09-15 10:05:00", text_len=120, reply_mid=1, reply_author=STAFF)
    _msg(db_path, 3, 11, "2026-09-15 10:06:00", text_len=120, reply_mid=1, reply_author=STAFF)
    _msg(db_path, 4, 12, "2026-09-15 10:07:00", text_len=600, reply_mid=1, reply_author=STAFF)
    _msg(db_path, 5, OWNER, "2026-09-15 11:00:00", text_len=5)   # пост суперадмина
    _msg(db_path, 6, 11, "2026-09-15 11:05:00", text_len=3, reply_mid=5, reply_author=OWNER)
    rows = _rows(_rules(db_path))
    assert rows[11]["comments"] == 2 and rows[11]["comment_points"] == 20
    assert rows[12]["valuable"] == 1 and rows[12]["comment_points"] == 20
    assert STAFF not in rows and OWNER not in rows


def test_reply_to_topic_root_is_not_a_comment(db_path):
    _msg(db_path, 1, STAFF, "2026-09-15 10:00:00")
    _msg(db_path, 2, 11, "2026-09-15 10:05:00", text_len=600)  # reply-поля NULL (корень темы)
    assert _rules(db_path)["rows"] == []


def test_comment_under_channel_post_counts(db_path):
    _msg(db_path, 1, -1001234, "2026-09-15 10:00:00", channel=1, text_len=300)
    _msg(db_path, 2, 11, "2026-09-15 10:05:00", reply_mid=1)  # автор цели — канал, NULL
    assert _rows(_rules(db_path))[11]["comment_points"] == 10


# ── приглашённые, соцсети, очно ──────────────────────────────────────────────────────────

def test_referrals_count_only_approved_of_current_season(db_path):
    _setting(db_path, "event_season", "YL 26/2")
    _member(db_path, 11)
    for tid in (21, 22, 23):
        _user(db_path, tid, referrer=11)
    _user(db_path, 24, referrer=11, status="pending", approved_at=None)
    _user(db_path, 25, referrer=11, season="YL 26/1")
    row = _rows(_rules(db_path))[11]
    assert row["referrals"] == 3 and row["referral_points"] == 15


def test_social_counts_picked_tasks_with_cap_and_hidden_without_tasks(db_path):
    # Одна действующая сдача на задание у человека (уникальный индекс) — поэтому 4 задания.
    _member(db_path, 11)
    for task in (7, 8, 9, 10):
        _submission(db_path, task, 11)
    _submission(db_path, 11, 11)                      # задание не выбрано для правила
    _submission(db_path, 12, 11, status="rejected")
    _submission(db_path, 13, 11, status="pending")
    result = _rules(db_path)
    assert "social" not in [c["key"] for c in result["columns"]]
    assert result["rows"] == []
    _setting(db_path, "chat_rules_social_tasks__city__spb", "\n".join(["7", "8", "9", "10", "12", "13"]))
    result = _rules(db_path)
    row = _rows(result)[11]
    assert row["social"] == 3 and row["social_points"] == 45
    assert "social" in [c["key"] for c in result["columns"]]


def test_city_empty_task_marker_overrides_global_list(db_path):
    _member(db_path, 11)
    _submission(db_path, 7, 11)
    _setting(db_path, "chat_rules_social_tasks", "7")
    assert _rows(_rules(db_path))[11]["social"] == 1
    _setting(db_path, "chat_rules_social_tasks__city__spb", "0")
    assert _rules(db_path)["rows"] == []


def test_checkins_count_forum_days_with_entrance_only(db_path):
    _member(db_path, 11)
    _checkin(db_path, 11, "entry", "2026-10-30")
    _checkin(db_path, 11, "entry", "2026-10-31")
    _checkin(db_path, 11, "session:5", "2026-10-31")
    row = _rows(_rules(db_path))[11]
    assert row["checkins"] == 2 and row["checkin_points"] == 40


# ── неделя, валюта, порядок, состав ──────────────────────────────────────────────────────

def test_prev_week_window_and_upgrade_shows_plus_ten(db_path):
    _msg(db_path, 1, STAFF, "2026-09-08 10:00:00")
    _msg(db_path, 2, 11, "2026-09-09 10:00:00", text_len=50, reply_mid=1, reply_author=STAFF)
    _msg(db_path, 3, 11, "2026-09-16 10:00:00", text_len=700, reply_mid=1, reply_author=STAFF)
    _msg(db_path, 4, 12, "2026-09-22 10:00:00", text_len=50, reply_mid=1, reply_author=STAFF)
    prev = _rows(_rules(db_path, "prev_week"))
    assert set(prev) == {11}
    assert prev[11]["comment_points"] == 10 and prev[11]["comments"] == 0
    assert prev[11]["valuable"] == 1
    assert set(_rows(_rules(db_path, "week"))) == {12}
    assert _rows(_rules(db_path, "all"))[11]["total"] == 20


def test_currency_override_and_order_by_total_then_name(db_path):
    _setting(db_path, "chat_rules_currency__city__spb", "LC")
    _exec(db_path, "INSERT INTO chat_usernames (telegram_id, username) VALUES (11, 'bob'), "
                   "(12, 'alice'), (13, 'carl')")
    _msg(db_path, 1, STAFF, "2026-09-15 10:00:00")
    _msg(db_path, 2, 11, "2026-09-15 10:01:00", reply_mid=1, reply_author=STAFF)
    _msg(db_path, 3, 12, "2026-09-15 10:02:00", reply_mid=1, reply_author=STAFF)
    _msg(db_path, 4, 13, "2026-09-15 10:03:00", text_len=900, reply_mid=1, reply_author=STAFF)
    result = _rules(db_path)
    assert result["currency"] == "LC"
    assert [r["display_name"] for r in result["rows"]] == ["@carl", "@alice", "@bob"]
    assert [r["place"] for r in result["rows"]] == [1, 2, 3]
    assert any("LC" in line for line in result["rules_text"])


def test_person_not_in_chat_and_never_wrote_here_is_not_listed(db_path):
    _setting(db_path, "event_season", "YL 26/2")
    _user(db_path, 21, referrer=77)  # 77 — из другого города, в этом чате нет
    _user(db_path, 22, referrer=11)
    _member(db_path, 11)
    _member(db_path, 88, status="left")
    _user(db_path, 23, referrer=88)
    assert set(_rows(_rules(db_path))) == {11}


def test_amount_zero_hides_column_and_awards_nothing(db_path):
    _setting(db_path, "chat_rules_referral_points__city__spb", "0")
    _setting(db_path, "event_season", "YL 26/2")
    _member(db_path, 11)
    _user(db_path, 21, referrer=11)
    result = _rules(db_path)
    assert "referrals" not in [c["key"] for c in result["columns"]]
    assert result["rows"] == []


def test_result_shape(db_path):
    result = _rules(db_path)
    for key in ("rows", "period", "periods", "currency", "rules", "rules_text", "columns",
                "bot_admin", "retention_days"):
        assert key in result
    assert result["currency"] == "баллы"
    assert [c["key"] for c in result["columns"]] == [
        "comments", "valuable", "referrals", "checkins",
    ]
