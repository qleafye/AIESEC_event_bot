"""Рейтинг чата на дашборде читает из базы только нужное окно.

Раньше на каждую карточку и любой период грузилась вся история чата (все сообщения и все
реакции). Теперь граница периода — в SQL (`ts >= ?`, индекс chat_id+ts), «Всё время»
ограничено сроком хранения, реакции берутся только к сообщениям окна.
"""
from __future__ import annotations

from dashboard import chat_rating
from dashboard import db as dash_db
from tests.test_dashboard_chat_rating_260927 import (  # noqa: F401 — фикстура db_path
    CHAT,
    CHAT_ID,
    NOW,
    _by_id,
    _msg,
    _rating,
    _react,
    _setting,
    db_path,
)


def _traced(path, call):
    statements = []
    with dash_db.read_conn(path) as conn:
        conn.set_trace_callback(statements.append)
        result = call(conn)
    return result, statements


def test_period_bound_is_pushed_into_sql(db_path):  # noqa: F811
    _msg(db_path, 1, 11, "2026-09-01 10:00:00")
    _msg(db_path, 2, 12, "2026-09-22 10:00:00")
    _react(db_path, 1, 13)
    result, sql = _traced(db_path, lambda conn: chat_rating.chat_rating(
        conn, CHAT, period="week", admin_ids=set(), now=NOW, registered_only=False))
    assert set(_by_id(result)) == {12}
    message_reads = [s for s in sql if "FROM chat_messages" in s and "SELECT message_id" in s]
    assert message_reads and all("ts >= '2026-09-21 00:00:00'" in s for s in message_reads)
    reaction_reads = [s for s in sql if "FROM chat_reactions" in s]
    assert reaction_reads and all("chat_messages" in s for s in reaction_reads)


def test_load_records_since_limits_rows_and_reactions(db_path):  # noqa: F811
    from datetime import date
    _msg(db_path, 1, 11, "2026-09-01 10:00:00")
    _msg(db_path, 2, 12, "2026-09-22 10:00:00")
    _react(db_path, 1, 13)
    _react(db_path, 2, 14)
    with dash_db.read_conn(db_path) as conn:
        records = chat_rating.load_records(conn, CHAT_ID, since=date(2026, 9, 21))
    assert [r.message_id for r in records] == [2]
    assert records[0].reaction_giver_ids == (14,)


def test_all_time_is_bounded_by_retention(db_path):  # noqa: F811
    _setting(db_path, "chat_rating_retention_days", "30")
    _msg(db_path, 1, 11, "2026-07-01 10:00:00")  # старше срока, чистка ещё не прошла
    _msg(db_path, 2, 12, "2026-09-20 10:00:00")
    assert set(_by_id(_rating(db_path, "all"))) == {12}


def test_week_result_matches_full_history_result(db_path):  # noqa: F811
    # Ответ в окне на старое сообщение всё равно даёт резонанс его автору.
    _msg(db_path, 1, 11, "2026-09-01 10:00:00")
    _msg(db_path, 2, 12, "2026-09-22 10:00:00", reply_mid=1, reply_author=11)
    _msg(db_path, 3, 12, "2026-09-22 10:01:00")
    rows = _by_id(_rating(db_path, "week"))
    assert rows[11]["resonance"] == 2.0
    assert rows[12]["messages"] == 2
