"""«✅ Взять» кандидата дозачитывает баллы за приглашённых, одобренных пока он был кандидатом
(владелец 09.10). Как ступени в `amb_status.take` → `_check_tiers`: одной строкой монет, без
двойного начисления, исключённые из зачёта не считаются."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from config import config
from database import amb_status_db as sdb
from database import db
from services import amb_journal, amb_status, amb_tiers
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
ADMIN = 777
REF = 100
CANDIDATE_SINCE = "2026-01-01 00:00:00"


def _run(coro):
    return asyncio.run(coro)


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, *, referrer=None, status="approved"):
    data = {"telegram_id": tid, "full_name": f"Delegate {tid}",
            "registration_date": "2026-09-01 00:00:00", "season": SEASON}
    if referrer:
        data["referrer_id"] = referrer
    _run(db.add_user(data))
    _run(db.set_user_status(tid, status))


def _coins(tid):
    return _sql("SELECT delta, reason FROM coins WHERE user_id = ? ORDER BY id", (tid,))


@pytest.fixture
def ready(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "test_amb_take_catchup.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("amb_team_selection_enabled", "on"))
    _run(db.set_setting("amb_join_mode", "selection"))
    _run(db.set_setting("ambassador_referral_coins", "10"))

    async def _no_tiers(*_a, **_k):
        return None

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _no_tiers)
    monkeypatch.setattr(amb_tiers, "check_tiers_for_invitees", _no_tiers)
    _seed(REF)
    _run(sdb.set_status(REF, "candidate", at=CANDIDATE_SINCE))
    # Двое одобрены, пока REF кандидат; одного потом исключили за накрутку; один одобрен до
    # того, как REF попросился в команду; один ещё не одобрен.
    for tid in (201, 202, 203, 204):
        _seed(tid, referrer=REF)
    _seed(205, referrer=REF, status="pending")
    _run(amb_journal.on_invitees_approved([201, 202, 203, 204]))
    _sql("UPDATE referral_credits SET credited_at = '2025-12-01 00:00:00' WHERE invitee_id = 204")
    assert _run(amb_journal.exclude(203, by=ADMIN, reason="накрутка"))
    assert _coins(REF) == []


def test_take_credits_invitees_approved_while_candidate(ready):
    assert _run(amb_status.take(REF, by=ADMIN)).outcome == "taken"
    assert _coins(REF) == [(20, "За приглашённых до вступления: 2")]
    rows = dict(_sql("SELECT invitee_id, coins FROM referral_credits"))
    assert rows == {201: 10, 202: 10, 203: 0, 204: 0}


def test_no_double_credit(ready):
    _run(amb_status.take(REF, by=ADMIN))
    assert _run(amb_status.take(REF, by=ADMIN)).outcome == "already_active"
    assert _run(amb_status.remove(REF, by=ADMIN))
    _run(amb_status.take(REF, by=ADMIN))  # из «вышел», не из кандидатов — дозачёта нет
    assert sum(d for d, _ in _coins(REF)) == 20
    # Новый приглашённый после вступления — обычный путь, баллы один раз.
    _seed(206, referrer=REF)
    _run(amb_journal.on_invitees_approved([206]))
    assert sum(d for d, _ in _coins(REF)) == 30


def test_exclusion_after_catchup_reverses_those_coins(ready):
    _run(amb_status.take(REF, by=ADMIN))
    assert _run(amb_journal.exclude(201, by=ADMIN, reason="накрутка"))
    assert sum(d for d, _ in _coins(REF)) == 10


def test_zero_coins_setting_credits_nothing(ready):
    _run(db.set_setting("ambassador_referral_coins", "0"))
    _run(amb_status.take(REF, by=ADMIN))
    assert _coins(REF) == []
    assert dict(_sql("SELECT invitee_id, coins FROM referral_credits"))[201] == 0
