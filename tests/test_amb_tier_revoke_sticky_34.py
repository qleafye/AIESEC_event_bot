"""«Снять ступень» липкое: снятую менеджером ступень (и все старшие) автоматика не возвращает,
пока менеджер не нажмёт «Вернуть ступень». Метка привязана к сезону и уходит с человеком."""
from __future__ import annotations

import asyncio
import logging

from config import config
from database import amb_tiers_db as tdb
from database import db
from services import amb_tiers
from tests._dbtpl import fast_init_db
from tests.test_amb_tiers_core_su5 import _make_ambassador, _seed_user, _sql

SEASON = "SU26"
BOSS = 7


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_amb_tier_revoke_sticky_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("amb_qualified_program", "on"))
    _run(db.set_setting("amb_count_deadline", "2099-01-01 00:00"))
    _make_ambassador(100)
    for tid in (201, 202, 203):  # порог ступени 1 — 1, ступени 2 — 3
        _seed_user(tid, referrer_id=100, status="approved")


def _tiers(tid=100):
    return [t["tier"] for t in _run(tdb.list_tiers(tid))]


def _granted():
    _run(amb_tiers.check_tiers([100]))
    return _tiers()


def test_revoked_tier_not_regranted_by_check_and_reconcile(tmp_path):
    _ready(tmp_path)
    assert _granted() == [1, 2]
    assert _run(amb_tiers.revoke_tier(100, 2, by=BOSS)) is True
    assert _tiers() == [1]
    assert _run(amb_tiers.check_tiers([100])) == []
    _run(amb_tiers.reconcile_tiers())
    assert _tiers() == [1]
    assert _sql("SELECT tier, season, revoked_by FROM amb_tier_revocations") == [(2, SEASON, BOSS)]


def test_revoking_lower_tier_blocks_higher_too(tmp_path):
    _ready(tmp_path)
    _granted()
    assert _run(amb_tiers.revoke_tier(100, 1, by=BOSS)) is True
    _run(amb_tiers.reconcile_tiers())
    assert _tiers() == [2]  # уже выданная старшая остаётся, новая не выдаётся
    _run(tdb.delete_tier_row(100, 2))
    _run(amb_tiers.reconcile_tiers())
    assert _tiers() == []


def test_unrevoke_regrants_and_logs(tmp_path, caplog):
    _ready(tmp_path)
    _granted()
    _run(amb_tiers.revoke_tier(100, 2, by=BOSS))
    with caplog.at_level(logging.INFO):
        new = _run(amb_tiers.unrevoke_tier(100, 2, by=BOSS))
    assert [r["tier"] for r in new] == [2]
    assert f"admin={BOSS} amb_tier_unrevoke tid=100 tier=2" in caplog.text
    assert _tiers() == [1, 2]
    assert _run(tdb.list_revocations(SEASON)) == []
    assert _run(amb_tiers.unrevoke_tier(100, 2, by=BOSS)) == []  # повторное нажатие


def test_revoke_twice_keeps_single_mark(tmp_path):
    _ready(tmp_path)
    _granted()
    assert _run(amb_tiers.revoke_tier(100, 2, by=BOSS)) is True
    assert _run(amb_tiers.revoke_tier(100, 2, by=BOSS)) is False
    assert len(_run(tdb.list_revocations(SEASON))) == 1


def test_other_season_unaffected(tmp_path):
    _ready(tmp_path)
    _granted()
    _run(amb_tiers.revoke_tier(100, 2, by=BOSS))
    assert _run(tdb.revoked_floor(100, SEASON)) == 2
    assert _run(tdb.revoked_floor(100, "SU27")) is None
    _run(db.set_setting("event_season", "SU27"))
    assert _run(tdb.list_revocations("SU27")) == []
    assert _run(tdb.claim_new_tiers(100, [2], "2026-10-01 10:00:00", {}, season="SU27")) == [
        {"tier": 2, "o2o_status": None}]


def test_backfill_preview_skips_revoked(tmp_path):
    _ready(tmp_path)
    _granted()
    _run(amb_tiers.revoke_tier(100, 2, by=BOSS))
    assert _run(amb_tiers.preview_backfill()) == []


def test_purge_user_removes_mark(tmp_path):
    _ready(tmp_path)
    _granted()
    _run(amb_tiers.revoke_tier(100, 2, by=BOSS))
    _run(db.purge_user(100))
    assert _sql("SELECT COUNT(*) FROM amb_tier_revocations") == [(0,)]


# ── экран: кнопка «Вернуть ступень» ──────────────────────────────────────────────────────

def test_ladder_shows_unrevoke_button_and_list_returns_tier(tmp_path):
    from handlers import admin_amb_tier_ladder as h
    from tests.test_amb_tiers_admin_su5 import ADMIN_ID, FakeCallback, _new_state
    _ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    _granted()

    def datas(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    cb = FakeCallback("ambl:main")
    _run(h.show_ladder(cb, _new_state()))
    assert "ambl_unrev" not in datas(cb.message.edits[-1][1])

    _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID))
    cb = FakeCallback("ambl:main")
    _run(h.show_ladder(cb, _new_state()))
    assert "ambl_unrev" in datas(cb.message.edits[-1][1])

    cb = FakeCallback("ambl_unrev")
    _run(h.unrevoke_list(cb))
    text, kb = cb.message.edits[-1]
    assert "ступень 2" in text and "ambl_unrev_go:100:2" in datas(kb)

    cb = FakeCallback("ambl_unrev_go:100:2")
    _run(h.unrevoke_go(cb))
    assert _tiers() == [1, 2]
    assert "возвращена" in cb.answers[-1][0]
    assert "ambl_unrev" not in datas(cb.message.edits[-1][1])


def test_revoke_confirmation_says_tier_will_not_return(tmp_path):
    from handlers import admin_amb_tier_ladder as h
    from tests.test_amb_tiers_admin_su5 import ADMIN_ID, FakeCallback, _new_state
    from handlers.states import AmbTierRevoke
    _ready(tmp_path)
    _granted()
    state = _new_state()
    _run(state.set_state(AmbTierRevoke.waiting_for_pick))
    _run(state.update_data(person_id=100, person_label="Амбассадор 100"))
    cb = FakeCallback("ambl_rev_pick:2")
    _run(h.revoke_pick(cb, state))
    text = cb.message.edits[-1][0]
    assert "↩️ Вернуть ступень" in text and "выдастся заново" not in text
