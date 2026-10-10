"""Отзыв зачёта, сверка журнала и бэкафилл журнала приглашённых."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from config import config
from database import amb_journal_db, db
from services.amb import amb_journal
from services.infra.timeutil import msk_now
from tests.test_referral_credit_32 import (
    _coins_rows,
    _make_ambassador,
    _ready,
    _run,
    _seed_user,
)


def _sql(sql, params=()):
    con = sqlite3.connect(config.DB_PATH)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _approve_at(tid, hours_ago):
    stamp = (msk_now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    _sql("UPDATE users SET approved_at = ? WHERE telegram_id = ?", (stamp, tid))


def _invitee(tid, referrer_id, hours_ago=1):
    _seed_user(tid, referrer_id=referrer_id, status="approved")
    _approve_at(tid, hours_ago)


def _credited_setup(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _invitee(2, 1)
    _run(amb_journal.on_invitees_approved([2]))


def test_revert_marks_revoked_keeps_coins(tmp_path):
    _credited_setup(tmp_path)
    assert len(_coins_rows(source="referral", user_id=1)) == 1
    _sql("UPDATE users SET status = 'pending' WHERE telegram_id = 2")
    res = _run(amb_journal.sync_revocations([2]))
    assert res["revoked"] == 1
    row = _run(amb_journal_db.get_row(2))
    assert row["revoked_at"] and row["revoked_reason"] == "status"
    assert len(_coins_rows(source="referral", user_id=1)) == 1


def test_reapproval_clears_revoked_without_second_coins(tmp_path):
    _credited_setup(tmp_path)
    _sql("UPDATE users SET status = 'pending' WHERE telegram_id = 2")
    _run(amb_journal.sync_revocations([2]))
    _sql("UPDATE users SET status = 'approved' WHERE telegram_id = 2")
    _run(amb_journal.on_invitees_approved([2]))
    row = _run(amb_journal_db.get_row(2))
    assert row["revoked_at"] is None and row["revoked_reason"] is None
    assert len(_coins_rows(source="referral", user_id=1)) == 1


def test_reconcile_revokes_and_clears(tmp_path):
    _credited_setup(tmp_path)
    _sql("UPDATE users SET status = 'pending' WHERE telegram_id = 2")
    assert _run(amb_journal.reconcile())["revoked"] == 1
    _sql("UPDATE users SET status = 'approved' WHERE telegram_id = 2")
    assert _run(amb_journal.reconcile())["cleared"] == 1
    assert _run(amb_journal_db.get_row(2))["revoked_at"] is None


def test_reconcile_adds_recent_missing_only(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _invitee(2, 1, hours_ago=2)
    _invitee(3, 1, hours_ago=5 * 24)
    res = _run(amb_journal.reconcile())
    assert res["added"] == 1
    row = _run(amb_journal_db.get_row(2))
    assert row["source"] == "reconcile" and row["coins"] == 10
    assert _run(amb_journal_db.get_row(3)) is None


def test_reconcile_never_raises(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(amb_journal_db, "mark_revoked", boom)
    monkeypatch.setattr(amb_journal_db, "missing_recent", boom)
    assert isinstance(_run(amb_journal.reconcile()), dict)


def test_revert_to_pending_hook_marks_revoked(tmp_path):
    from services.applications import revert_pending
    _credited_setup(tmp_path)
    report = _run(revert_pending.revert_to_pending(2, by_admin=1, notify=False))
    assert report["ok"]
    assert _run(amb_journal_db.get_row(2))["revoked_at"]
    assert len(_coins_rows(source="referral", user_id=1)) == 1


def test_scheduler_registers_reconcile_job():
    import inspect

    import services.scheduler as scheduler

    assert '"amb_journal_reconcile"' in inspect.getsource(scheduler)
    assert scheduler._amb_journal_reconcile_job.__qualname__ == "_amb_journal_reconcile_job"


# ── бэкафилл ────────────────────────────────────────────────────────────────────────────────

def _backfill(**kw):
    from tools.backfill_amb_journal import backfill_journal
    return _run(backfill_journal(**kw))


def _backfill_setup(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _invitee(2, 1, hours_ago=10 * 24)
    _invitee(3, 1, hours_ago=10 * 24)
    _invitee(4, 1, hours_ago=10 * 24)
    # уже есть строка с начислением — не трогается
    _run(amb_journal.on_invitees_approved([4]))


def test_backfill_dry_run_writes_nothing(tmp_path):
    _backfill_setup(tmp_path)
    res = _backfill(apply=False, season=None)
    assert res["rows"] == 2 and res["inserted"] == 0
    assert res["breakdown"][0]["referrer_id"] == 1 and res["breakdown"][0]["invitees"] == 2
    assert _run(amb_journal_db.get_row(2)) is None


def test_backfill_apply_idempotent_no_coins(tmp_path):
    _backfill_setup(tmp_path)
    coins_before = _coins_rows()
    before4 = _run(amb_journal_db.get_row(4))
    res = _backfill(apply=True, season="YL 26/2")
    assert res["inserted"] == 2
    row = _run(amb_journal_db.get_row(2))
    assert row["coins"] == 0 and row["source"] == "backfill"
    assert row["season"] == "YL 26/2" and row["referrer_was_ambassador"] == 1
    assert _coins_rows() == coins_before
    assert _run(amb_journal_db.get_row(4)) == before4
    again = _backfill(apply=True, season="YL 26/2")
    assert again["rows"] == 0 and again["inserted"] == 0


def test_backfill_reports_missing_schema(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def no_schema():
        return False

    monkeypatch.setattr(amb_journal_db, "has_journal_schema", no_schema)
    assert _backfill(apply=True, season=None)["schema"] is False
