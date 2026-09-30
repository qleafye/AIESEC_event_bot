"""Ручное закрепление приглашённого за пригласившим (services/amb_journal.manual_attach)."""
from __future__ import annotations

import sqlite3

from config import config
from database import amb_journal_db, db
from services import amb_journal
from tests.test_referral_credit_32 import (
    _coins_rows,
    _make_ambassador,
    _ready,
    _run,
    _seed_user,
)


def _referrer_of(tid):
    return _run(db.get_user(tid)).get("referrer_id")


def test_attach_approved_writes_journal_row_and_coins(tmp_path):
    _ready(tmp_path)
    _make_ambassador(10)
    _seed_user(20, status="approved")
    res = _run(amb_journal.manual_attach(20, 10, by=1, note="скрины в чате"))
    assert res["ok"] and res["approved"]
    assert _referrer_of(20) == 10
    row = _run(amb_journal_db.get_row(20))
    assert row["source"] == "manual" and row["manual_by"] == 1
    assert row["manual_note"] == "скрины в чате"
    assert row["referrer_id"] == 10
    assert _run(amb_journal_db.pop_manual_attach(20)) is None
    if res["coins"]:
        assert _coins_rows(source="referral", user_id=10)


def test_attach_pending_saved_then_row_on_approval(tmp_path):
    _ready(tmp_path)
    _make_ambassador(10)
    _seed_user(20, status="pending")
    res = _run(amb_journal.manual_attach(20, 10, by=7, note=None))
    assert res["ok"] and not res["approved"]
    assert _referrer_of(20) == 10
    assert _run(amb_journal_db.get_row(20)) is None
    _run(db.set_user_status(20, "approved"))
    _run(amb_journal.on_invitees_approved([20], changed_by=1))
    row = _run(amb_journal_db.get_row(20))
    assert row["source"] == "manual" and row["manual_by"] == 7
    assert _run(amb_journal_db.pop_manual_attach(20)) is None


def test_attach_errors(tmp_path):
    _ready(tmp_path)
    _make_ambassador(10)
    _make_ambassador(11)
    _seed_user(20, status="approved")
    assert _run(amb_journal.manual_attach(20, 20, by=1))["error"] == "self"
    assert _run(amb_journal.manual_attach(20, 999, by=1))["error"] == "no_user"
    assert _run(amb_journal.manual_attach(999, 10, by=1))["error"] == "no_user"
    assert _run(amb_journal.manual_attach(20, 10, by=1))["ok"]
    again = _run(amb_journal.manual_attach(20, 11, by=1))
    assert again["error"] == "already" and again["current_referrer"] == 10
    assert _referrer_of(20) == 10


def test_attach_writes_answer_history(tmp_path):
    _ready(tmp_path)
    _make_ambassador(10)
    _seed_user(20, status="approved")
    _run(amb_journal.manual_attach(20, 10, by=1))
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT source, changes FROM reg_answer_history WHERE telegram_id = 20"
        ).fetchall()
    finally:
        con.close()
    assert rows and rows[0][0] == "admin" and "referrer_id" in rows[0][1]


def test_purge_anonymizes_journal_row_and_drops_pending(tmp_path):
    _ready(tmp_path)
    _make_ambassador(10)
    _seed_user(20, status="approved")
    _seed_user(21, status="pending")
    _run(amb_journal.manual_attach(20, 10, by=1, note="секрет"))
    _run(amb_journal.manual_attach(21, 10, by=1, note="секрет"))
    before = _run(db.count_user_footprint(20))
    assert before["referral_credits"] >= 1
    after = _run(db.purge_user(20))
    assert after["referral_credits"] == before["referral_credits"]
    row = _run(amb_journal_db.get_row(20))
    assert row is not None
    assert row["manual_note"] is None and row["manual_by"] is None
    _run(db.purge_user(21))
    assert _run(amb_journal_db.pop_manual_attach(21)) is None
