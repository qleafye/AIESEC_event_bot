"""Журнал зачётов приглашённых и единая точка «приглашённого одобрили» (services/amb_journal)."""
from __future__ import annotations

import sqlite3

from config import config
from database import amb_journal_db, amb_tiers_db, db
from services import amb_journal
from tests.test_referral_credit_32 import (
    _coins_rows,
    _make_ambassador,
    _ready,
    _run,
    _seed_user,
)

_NEW_COLUMNS = {
    "season", "referrer_was_ambassador", "revoked_at", "revoked_reason", "excluded_at",
    "excluded_by", "exclude_reason", "manual_by", "manual_note", "reversal_coin_id",
}


def _approved_invitee(tid, referrer_id, **kw):
    _seed_user(tid, referrer_id=referrer_id, status="approved", **kw)


def test_schema_has_journal_columns_and_old_rows_untouched(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(referral_credits)")}
    finally:
        con.close()
    assert _NEW_COLUMNS <= cols
    # старая строка (без новых колонок) читается, NULL в новых полях
    con = sqlite3.connect(config.DB_PATH)
    try:
        con.execute(
            "INSERT INTO referral_credits (invitee_id, referrer_id, coins, wave_id, credited_at, "
            "source) VALUES (1, 2, 5, NULL, '2026-01-01 00:00:00', 'approval')"
        )
        con.commit()
    finally:
        con.close()
    row = _run(amb_journal_db.get_row(1))
    assert row["coins"] == 5 and row["referrer_was_ambassador"] is None


def test_regular_delegate_invitee_gets_row_with_zero_and_no_coins(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _seed_user(1, status="approved")
    _approved_invitee(2, 1)
    summary = _run(amb_journal.on_invitees_approved([2]))
    assert summary == {"credited": 0, "coins": 0, "ambassadors": 0}
    row = _run(amb_journal_db.get_row(2))
    assert row["coins"] == 0 and row["referrer_was_ambassador"] == 0 and row["wave_id"] is None
    assert _coins_rows(source="referral") == []


def test_ambassador_invitee_gets_points_and_coin_row(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _approved_invitee(2, 1)
    summary = _run(amb_journal.on_invitees_approved([2]))
    assert summary == {"credited": 1, "coins": 10, "ambassadors": 1}
    row = _run(amb_journal_db.get_row(2))
    assert row["coins"] == 10 and row["referrer_was_ambassador"] == 1
    assert [(r[0], r[1]) for r in _coins_rows(source="referral")] == [(1, 10)]


def test_zero_coins_setting_row_without_coins(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "0"))
    _make_ambassador(1)
    _approved_invitee(2, 1)
    _run(amb_journal.on_invitees_approved([2]))
    row = _run(amb_journal_db.get_row(2))
    assert row["coins"] == 0 and row["referrer_was_ambassador"] == 1
    assert _coins_rows(source="referral") == []


def test_excluded_invitee_row_has_mark_and_no_points(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _approved_invitee(2, 1)
    _run(amb_tiers_db.exclude_invitee(2, "альт-аккаунт", 99, "2026-09-01 00:00:00"))
    _run(amb_journal.on_invitees_approved([2]))
    row = _run(amb_journal_db.get_row(2))
    assert row["excluded_at"] and row["coins"] == 0
    assert _coins_rows(source="referral") == []


def test_repeat_call_adds_nothing(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _approved_invitee(2, 1)
    _run(amb_journal.on_invitees_approved([2]))
    summary = _run(amb_journal.on_invitees_approved([2]))
    assert summary["credited"] == 0
    assert len(_coins_rows(source="referral")) == 1


def test_no_referrer_or_not_approved_does_nothing(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _approved_invitee(2, None)
    _seed_user(3, referrer_id=1, status="pending")
    _run(amb_journal.on_invitees_approved([2, 3]))
    assert _run(amb_journal_db.get_row(2)) is None
    assert _run(amb_journal_db.get_row(3)) is None


def test_self_referral_skipped(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _run(db.add_user({"telegram_id": 1, "full_name": "Self", "referrer_id": 1,
                       "registration_date": "2026-09-01 00:00:00"}))
    _run(db.set_user_status(1, "approved"))
    _run(amb_journal.on_invitees_approved([1]))
    assert _run(amb_journal_db.get_row(1)) is None


def test_reapproval_clears_revoked_mark_without_second_points(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _approved_invitee(2, 1)
    _run(amb_journal.on_invitees_approved([2]))
    con = sqlite3.connect(config.DB_PATH)
    try:
        con.execute(
            "UPDATE referral_credits SET revoked_at='2026-09-02 00:00:00', "
            "revoked_reason='отказ' WHERE invitee_id=2"
        )
        con.commit()
    finally:
        con.close()
    _run(amb_journal.on_invitees_approved([2]))
    row = _run(amb_journal_db.get_row(2))
    assert row["revoked_at"] is None and row["revoked_reason"] is None
    assert len(_coins_rows(source="referral")) == 1


def test_bulk_summary_and_single_calls_of_followups(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _make_ambassador(5)
    for tid, ref in ((2, 1), (3, 1), (4, 5)):
        _approved_invitee(tid, ref)
    calls = {"slot": [], "tiers": []}

    async def fake_slot(ids):
        calls["slot"].append(list(ids))

    async def fake_tiers(ids):
        calls["tiers"].append(list(ids))

    from services import amb_status, amb_tiers
    monkeypatch.setattr(amb_status, "on_applications_approved", fake_slot)
    monkeypatch.setattr(amb_tiers, "check_tiers_for_invitees", fake_tiers)
    summary = _run(amb_journal.on_invitees_approved([2, 3, 4]))
    assert summary == {"credited": 3, "coins": 30, "ambassadors": 2}
    assert calls == {"slot": [[2, 3, 4]], "tiers": [[2, 3, 4]]}


def test_failure_of_followups_is_not_raised(tmp_path, monkeypatch):
    _ready(tmp_path)
    _approved_invitee(2, None)

    async def boom(ids):
        raise RuntimeError("сбой")

    from services import amb_status, amb_tiers
    monkeypatch.setattr(amb_status, "on_applications_approved", boom)
    monkeypatch.setattr(amb_tiers, "check_tiers_for_invitees", boom)
    assert _run(amb_journal.on_invitees_approved([2]))["credited"] == 0
