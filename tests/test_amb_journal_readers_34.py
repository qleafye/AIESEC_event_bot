"""Читатели журнала зачётов: рейтинг чата «приведённые», счётчики дашборда и снимка кейса."""
import sqlite3

import pytest

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from dashboard.queries import Scope, _ambassador_funnel
from tests._dbtpl import fast_init_db

SEASON = "SU26"


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "readers.db")
    config.DB_PATH = p
    fast_init_db()
    _x(p, "INSERT INTO bot_settings (key, value) VALUES ('event_season', ?)", (SEASON,))
    return p


def _x(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _user(path, uid, *, referrer=None, status="approved", season=SEASON, amb=0):
    _x(path, "INSERT INTO users (telegram_id, username, full_name, status, referrer_id, season, "
             "approved_at, is_ambassador) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
       (uid, f"u{uid}", f"U{uid}", status, referrer, season, "2026-09-24 12:00:00", amb))


def _credit(path, invitee, referrer, *, coins=0, was_amb=1, excluded=None, revoked=None):
    _x(path, "INSERT INTO referral_credits (invitee_id, referrer_id, coins, credited_at, season, "
             "referrer_was_ambassador, excluded_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
       (invitee, referrer, coins, "2026-09-30 10:00:00", SEASON, was_amb, excluded, revoked))


def _dates(path):
    with dash_db.read_conn(config.DB_PATH) as conn:
        return chat_rating._referral_dates(conn)


def _count(d, referrer):
    return len(d.get(referrer, []))


def test_plain_delegate_invitee_counts(path):
    _user(path, 1)
    _user(path, 2, referrer=1)
    _credit(path, 2, 1, coins=0, was_amb=0)
    assert _count(_dates(path), 1) == 1


def test_pending_invitee_drops_then_returns(path):
    _user(path, 1)
    _user(path, 2, referrer=1)
    _credit(path, 2, 1)
    _x(path, "UPDATE users SET status='pending' WHERE telegram_id=2")
    assert _count(_dates(path), 1) == 0
    _x(path, "UPDATE users SET status='approved' WHERE telegram_id=2")
    assert _count(_dates(path), 1) == 1


def test_excluded_not_counted(path):
    _user(path, 1)
    _user(path, 2, referrer=1)
    _credit(path, 2, 1, excluded="2026-09-30 11:00:00")
    assert _count(_dates(path), 1) == 0


def test_other_season_not_counted(path):
    _user(path, 1)
    _user(path, 2, referrer=1, season="OLD")
    _credit(path, 2, 1)
    assert _count(_dates(path), 1) == 0


def test_incomplete_journal_falls_back_to_users(path):
    _user(path, 1)
    _user(path, 2, referrer=1)
    _user(path, 3, referrer=1)
    _credit(path, 2, 1)  # у 3 строки журнала нет -> фолбэк по users
    assert _count(_dates(path), 1) == 2
    _credit(path, 3, 1)  # журнал полный -> по журналу
    assert _count(_dates(path), 1) == 2
    _x(path, "UPDATE referral_credits SET excluded_at='x' WHERE invitee_id=3")
    assert _count(_dates(path), 1) == 1


def test_fallback_skips_exclusions(path):
    _user(path, 1)
    _user(path, 2, referrer=1)
    _user(path, 3, referrer=1)
    _x(path, "INSERT INTO ambassador_exclusions (invitee_id, reason, excluded_at) VALUES (3, 'x', '2026-09-30')")
    assert _count(_dates(path), 1) == 1


def test_parity_with_journal_sql(path):
    _user(path, 1)
    for i in range(2, 6):
        _user(path, i, referrer=1)
        _credit(path, i, 1)
    _x(path, "UPDATE users SET status='pending' WHERE telegram_id=5")
    conn = sqlite3.connect(path)
    etalon = conn.execute(
        "SELECT COUNT(*) FROM referral_credits rc JOIN users u ON u.telegram_id=rc.invitee_id "
        "WHERE u.status='approved' AND rc.excluded_at IS NULL AND rc.referrer_id=1").fetchone()[0]
    conn.close()
    assert _count(_dates(path), 1) == etalon == 3


def test_old_schema_without_journal_columns(path):
    _user(path, 1)
    _user(path, 2, referrer=1)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE referral_credits")
    conn.execute("CREATE TABLE referral_credits (invitee_id INTEGER PRIMARY KEY, "
                 "referrer_id INTEGER, coins INTEGER, credited_at TEXT)")
    conn.commit()
    conn.close()
    assert _count(_dates(path), 1) == 1


def test_dashboard_credited_filters(path):
    _user(path, 1, amb=1)
    for i, kw in ((2, {}), (3, {"was_amb": 0}), (4, {"excluded": "x"}), (5, {"revoked": "x"}),
                  (6, {"was_amb": None})):
        _user(path, i, referrer=1)
        _credit(path, i, 1, coins=50, **kw)
    with dash_db.read_conn(config.DB_PATH) as conn:
        rows = _ambassador_funnel(conn, Scope())
    assert rows[0]["credited"] == 2


def test_case_snapshot_filters(path):
    from tools import case_snapshot as cs
    _user(path, 1, amb=1)
    for i, kw in ((2, {}), (3, {"was_amb": 0}), (4, {"excluded": "x"}), (5, {"revoked": "x"})):
        _user(path, i, referrer=1)
        _credit(path, i, 1, coins=50, **kw)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    sc = cs._Schema(conn)
    out = cs.m12_ambassadors(sc, cs.Scope(sc, None, None))
    conn.close()
    assert out["referral_credits"] == 1
    assert out["referrers"] == 1
