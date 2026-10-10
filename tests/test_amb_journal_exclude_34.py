"""Исключение приглашённого за накрутку: обратная строка монет, очки волны, маскировка имён,
возврат в зачёт, удаление аккаунта."""
from __future__ import annotations

import sqlite3

from config import config
from database import amb_journal_db, amb_tiers_db, db
from services.amb import amb_journal, amb_progress
from tests.test_referral_credit_32 import (
    _active_wave,
    _coins_rows,
    _make_ambassador,
    _ready,
    _run,
    _seed_user,
)


def _setup(tmp_path, *, coins="10", wave=False):
    _ready(tmp_path, "test_amb_journal_exclude_34.db")
    _run(db.set_setting("ambassador_referral_coins", coins))
    wave_id = _active_wave() if wave else None
    _make_ambassador(1, full_name="Анна Смирнова")
    _seed_user(2, referrer_id=1, status="approved", full_name="Иван Петров")
    _run(amb_journal.on_invitees_approved([2]))
    return wave_id


def _balance(tid):
    return sum(r[1] for r in _coins_rows(user_id=tid))


def test_exclude_writes_reversal_row_and_marks_journal(tmp_path):
    _setup(tmp_path)
    assert _balance(1) == 10
    assert _run(amb_journal.exclude(2, by=99, reason="накрутка")) is True
    assert _balance(1) == 0
    rev = _coins_rows(source="referral_reversal", user_id=1)
    assert len(rev) == 1 and rev[0][1] == -10
    assert rev[0][2] == "Снят с зачёта приглашённый: Иван Петров"
    row = _run(amb_journal_db.get_row(2))
    assert row["excluded_at"] and row["excluded_by"] == 99 and row["exclude_reason"] is None
    assert row["reversal_coin_id"] is not None
    assert _run(amb_tiers_db.get_exclusion(2))["reason"] == "накрутка"
    # начисление осталось в истории — не удалено
    assert len(_coins_rows(source="referral", user_id=1)) == 1


def test_repeat_exclude_does_not_reverse_twice(tmp_path):
    _setup(tmp_path)
    assert _run(amb_journal.exclude(2, by=99, reason="a")) is True
    assert _run(amb_journal.exclude(2, by=99, reason="b")) is False
    assert len(_coins_rows(source="referral_reversal")) == 1
    assert _balance(1) == 0


def test_exclude_zero_coins_writes_no_coin_row(tmp_path):
    _setup(tmp_path, coins="0")
    assert _run(amb_journal.exclude(2, by=99, reason="x")) is True
    assert _coins_rows(source="referral_reversal") == []
    assert _run(amb_journal_db.get_row(2))["excluded_at"]


def test_exclude_not_yet_approved_only_exclusion_then_zero_credit(tmp_path):
    _ready(tmp_path, "test_amb_journal_exclude_34.db")
    _run(db.set_setting("ambassador_referral_coins", "10"))
    _make_ambassador(1)
    _seed_user(2, referrer_id=1, status="pending")
    assert _run(amb_journal.exclude(2, by=99, reason="x")) is True
    assert _run(amb_journal_db.get_row(2)) is None
    _run(db.set_user_status(2, "approved"))
    _run(amb_journal.on_invitees_approved([2]))
    row = _run(amb_journal_db.get_row(2))
    assert row["coins"] == 0 and row["excluded_at"]
    assert _coins_rows(user_id=1) == []


def test_unexclude_restores_with_new_row_and_second_exclude_works(tmp_path):
    _setup(tmp_path)
    _run(amb_journal.exclude(2, by=99, reason="x"))
    assert _run(amb_journal.unexclude(2, by=98)) is True
    assert _balance(1) == 10
    assert len(_coins_rows(source="referral_reversal")) == 1  # обратная строка не удалена
    restore = [r for r in _coins_rows(source="referral", user_id=1) if r[1] == 10]
    assert any(r[2] == "Возвращён в зачёт приглашённый: Иван Петров" for r in restore)
    row = _run(amb_journal_db.get_row(2))
    assert row["excluded_at"] is None and row["reversal_coin_id"] is None
    assert _run(amb_tiers_db.get_exclusion(2)) is None
    assert _run(amb_journal.unexclude(2, by=98)) is False
    assert _balance(1) == 10
    # повторное исключение после возврата списывает ещё раз ровно один раз
    assert _run(amb_journal.exclude(2, by=99, reason="y")) is True
    assert _balance(1) == 0


def test_wave_points_drop_and_return(tmp_path):
    wave_id = _setup(tmp_path, wave=True)
    assert _run(db.sum_referral_coins_for_wave(wave_id)) == {1: 10}
    _run(amb_journal.exclude(2, by=99, reason="x"))
    assert _run(db.sum_referral_coins_for_wave(wave_id)) == {}
    _run(amb_journal.unexclude(2, by=99))
    assert _run(db.sum_referral_coins_for_wave(wave_id)) == {1: 10}


def test_revoked_credit_still_counts_in_wave(tmp_path):
    wave_id = _setup(tmp_path, wave=True)
    _run(db.set_user_status(2, "rejected"))
    _run(amb_journal.sync_revocations())
    assert _run(db.sum_referral_coins_for_wave(wave_id)) == {1: 10}


def test_dashboard_wave_points_parity(tmp_path):
    from dashboard import queries
    wave_id = _setup(tmp_path, wave=True)
    _seed_user(3, referrer_id=1, status="approved", full_name="Мария Орлова")
    _run(amb_journal.on_invitees_approved([3]))
    _run(amb_journal.exclude(2, by=99, reason="x"))
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        wave = dict(conn.execute("SELECT * FROM ambassador_waves WHERE id = ?", (wave_id,)).fetchone())
        rows = queries._ambassador_wave_rating(conn, queries.Scope(), wave)
    finally:
        conn.close()
    bot = _run(db.sum_referral_coins_for_wave(wave_id))
    dash = {r["telegram_id"]: r["points"] for r in rows}
    assert bot == {1: 10}
    assert dash.get(1) == 10


def test_masking_covers_reversal_rows(tmp_path):
    _setup(tmp_path)
    _run(amb_journal.exclude(2, by=99, reason="x"))
    _run(db.set_setting("amb_hide_invitee_names", "on"))

    async def tr_key(key):
        return {"amb_invitee_masked_label_text": "Приглашённый №{n}"}[key]

    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM coins WHERE user_id = 1 ORDER BY id")]
    con.close()
    masked = _run(amb_progress.mask_referral_coin_rows(1, rows, tr_key))
    assert masked and all("Иван" not in (r.get("reason") or "") for r in masked)
    assert any(r["source"] == "referral_reversal" and r["reason"].startswith("Приглашённый")
               for r in masked)


def test_purge_removes_exclusion_with_account(tmp_path):
    _setup(tmp_path)
    _run(amb_journal.exclude(2, by=99, reason="личное"))
    assert any(t[0] == "ambassador_exclusions" for t in db.USER_PURGE_TABLES)
    _run(db.purge_user(2))
    assert _run(amb_tiers_db.get_exclusion(2)) is None
    # начисленное и обратная строка у пригласившего остаются
    assert len(_coins_rows(source="referral_reversal", user_id=1)) == 1
