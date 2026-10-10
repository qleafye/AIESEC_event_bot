"""Правила входа в команду амбассадоров — `services/amb_status.py`: режим входа, лимит мест,
место только с одобренной заявкой, отказанные, «Взять», выход, вывод менеджером, швы
одобрения и сверка мест, состояние для «Моя ссылка».

pytest-asyncio в окружении нет — async через `asyncio.run()`, временная БД — тот же приём,
что `tests/test_amb_status_34.py::_ready`.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import ast
import asyncio
import sqlite3
from pathlib import Path

import pytest

from config import config
from database import amb_status_db as sdb
from database import db
from services import amb_status, amb_tiers
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
AT = "2026-09-30 12:00:00"
ADMIN = 777
REPO = REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "test_amb_status_service_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    # Правила отбора и лимита живут только при включённом модуле «🤝 Отбор амбассадоров».
    _run(db.set_setting("amb_team_selection_enabled", "on"))
    calls: list[int] = []

    async def _fake_tiers(tid):
        calls.append(int(tid))

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _fake_tiers)
    return calls


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, *, status="approved", season=SEASON):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "season": season,
    }))
    if status:
        _run(db.set_user_status(tid, status))


def _mode(mode):
    _run(db.set_setting("amb_join_mode", mode))


def _limit(n):
    _run(db.set_setting("amb_slots_limit", str(n)))


def _st(tid):
    return _run(sdb.get_status(tid))


def _is_amb(tid):
    return _sql("SELECT is_ambassador FROM users WHERE telegram_id = ?", (tid,))[0][0]


def _fill_slots(n, *, start=5000):
    """n действующих амбассадоров с местом (одобренная заявка текущего сезона)."""
    for i in range(n):
        tid = start + i
        _seed(tid)
        _run(sdb.set_status(tid, "active", at=AT))
        assert _run(sdb.try_claim_slot(tid, limit=0, season=SEASON, at=AT))


# ── Настройки и дефолты ───────────────────────────────────────────────────────────────────────

def test_defaults_keep_old_behaviour(ready):
    assert _run(amb_status.join_mode()) == "instant"
    assert _run(amb_status.slots_limit()) == 0
    assert _run(amb_status.slots_full()) is False
    assert _run(amb_status.offer_open()) is True


def test_garbage_mode_is_instant_and_negative_limit_is_zero(ready):
    _run(db.set_setting("amb_join_mode", "whatever"))
    _run(db.set_setting("amb_slots_limit", "-5"))
    assert _run(amb_status.join_mode()) == "instant"
    assert _run(amb_status.slots_limit()) == 0


def test_slot_counter(ready):
    _limit(17)
    _fill_slots(3)
    assert _run(amb_status.slot_counter()) == (3, 17)


# ── request_join: режим «сразу по кнопке» ───────────────────────────────────────────────────

def test_instant_no_limit_joins_and_checks_tiers(ready):
    _seed(1)
    res = _run(amb_status.request_join(1, source="button_bot"))
    assert res.outcome == "active"
    assert _is_amb(1) == 1
    assert ready == [1]
    again = _run(amb_status.request_join(1, source="button_bot"))
    assert again.outcome == "already_active"
    assert ready == [1]


def test_instant_approved_gets_slot_unapproved_joins_without_pack(ready):
    _limit(17)
    _fill_slots(3)
    _seed(1)
    _seed(2, status="pending")
    res = _run(amb_status.request_join(1, source="form"))
    assert (res.outcome, res.slot) == ("active", True)
    res2 = _run(amb_status.request_join(2, source="form"))
    assert (res2.outcome, res2.slot) == ("active", False)
    assert _st(2)["status"] == "active" and _st(2)["slot_at"] is None
    assert _run(amb_status.slot_counter()) == (4, 17)


def test_instant_migrated_candidate_can_join(ready):
    _seed(1)
    _run(sdb.set_status(1, "candidate", at=AT))
    assert _run(amb_status.offer_open(1)) is True
    assert _run(amb_status.request_join(1, source="my_link")).outcome == "active"


def test_unknown_user_is_no_user(ready):
    assert _run(amb_status.request_join(424242, source="button_app")).outcome == "no_user"


# ── request_join: режим отбора ───────────────────────────────────────────────────────────────

def test_selection_makes_candidate_only(ready):
    _mode("selection")
    _seed(1)
    res = _run(amb_status.request_join(1, source="button_bot"))
    assert res.outcome == "candidate"
    assert _st(1)["status"] == "candidate"
    assert _is_amb(1) == 0
    assert ready == []
    assert _run(amb_status.request_join(1, source="form")).outcome == "already_candidate"
    assert _st(1)["status"] == "candidate"


def test_selection_left_becomes_candidate_active_stays(ready):
    _mode("selection")
    _seed(1)
    _seed(2)
    _run(sdb.set_status(1, "left", at=AT))
    _run(sdb.set_status(2, "active", at=AT))
    assert _run(amb_status.request_join(1, source="my_link")).outcome == "candidate"
    assert _run(amb_status.request_join(2, source="my_link")).outcome == "already_active"
    assert _st(2)["status"] == "active"


def test_declined_gets_no_offer_and_no_write(ready):
    for mode in ("selection", "instant"):
        _mode(mode)
        tid = 10 if mode == "selection" else 11
        _seed(tid)
        _run(sdb.set_status(tid, "declined", at=AT))
        assert _run(amb_status.offer_open(tid)) is False
        assert _run(amb_status.request_join(tid, source="button_bot")).outcome == "declined"
        assert _st(tid)["status"] == "declined"
        assert _st(tid)["status_at"] == AT


# ── Лимит ────────────────────────────────────────────────────────────────────────────────────

def test_full_limit_closes_offer_and_join_writes_nothing(ready):
    _limit(17)
    _fill_slots(17)
    _seed(1)
    assert _run(amb_status.slots_full()) is True
    assert _run(amb_status.offer_open()) is False
    assert _run(amb_status.offer_open(1)) is False
    for mode in ("instant", "selection"):
        _mode(mode)
        assert _run(amb_status.request_join(1, source="button_bot")).outcome == "full"
        assert _st(1)["status"] == "none"
    assert _run(amb_status.delegate_state(1)) == "full"


def test_leave_without_pack_reopens_offer(ready):
    _limit(17)
    _fill_slots(17)
    assert _run(amb_status.offer_open()) is False
    assert _run(amb_status.leave(5000)) is True
    assert _st(5000)["status"] == "left"
    assert _run(amb_status.offer_open()) is True
    assert _run(amb_status.leave(5000)) is False


def test_leave_with_pack_keeps_slot(ready):
    _limit(17)
    _fill_slots(17)
    _run(sdb.set_pack(5000, True, at=AT))
    assert _run(amb_status.leave(5000)) is True
    assert _run(amb_status.offer_open()) is False


def test_active_offer_open_even_when_full(ready):
    _limit(1)
    _fill_slots(1)
    assert _run(amb_status.offer_open(5000)) is True


def test_candidate_offer_by_mode(ready):
    _seed(1)
    _run(sdb.set_status(1, "candidate", at=AT))
    _mode("selection")
    assert _run(amb_status.offer_open(1)) is False
    _mode("instant")
    assert _run(amb_status.offer_open(1)) is True


def test_offer_open_fail_open(ready, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sdb, "get_status", _boom)
    monkeypatch.setattr(sdb, "slots_taken", _boom)
    _limit(5)
    assert _run(amb_status.offer_open(1)) is True
    assert _run(amb_status.offer_open()) is True


def test_no_limit_does_not_count_slots(ready, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("COUNT без лимита")

    monkeypatch.setattr(sdb, "slots_taken", _boom)
    assert _run(amb_status.slots_full()) is False
    assert _run(amb_status.offer_open()) is True


# ── take / remove ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("start", ["declined", "candidate", None, "left"])
def test_take_any_delegate(ready, start):
    _limit(17)
    _seed(1)
    if start:
        _run(sdb.set_status(1, start, at=AT))
    res = _run(amb_status.take(1, by=ADMIN))
    assert (res.outcome, res.slot) == ("taken", True)
    assert _st(1)["status"] == "active" and _is_amb(1) == 1
    assert _sql("SELECT ambassador_status_by FROM users WHERE telegram_id = 1")[0][0] == ADMIN
    assert ready == [1]
    assert _run(amb_status.take(1, by=ADMIN)).outcome == "already_active"


def test_take_over_limit_is_active_without_pack(ready):
    _limit(2)
    _fill_slots(2)
    _seed(1)
    res = _run(amb_status.take(1, by=ADMIN))
    assert (res.outcome, res.slot) == ("taken", False)
    assert _st(1)["status"] == "active" and _st(1)["slot_at"] is None
    assert _run(amb_status.delegate_state(1)) == "active_no_pack"


def test_take_unknown_user(ready):
    assert _run(amb_status.take(999999, by=ADMIN)).outcome == "no_user"


def test_remove_by_manager(ready):
    _limit(17)
    _fill_slots(2)
    assert _run(amb_status.remove(5000, by=ADMIN)) is True
    assert _st(5000)["status"] == "left" and _st(5000)["slot_at"] is None
    assert _sql("SELECT ambassador_status_by FROM users WHERE telegram_id = 5000")[0][0] == ADMIN
    _run(sdb.set_pack(5001, True, at=AT))
    assert _run(amb_status.remove(5001, by=ADMIN)) is True
    assert _st(5001)["slot_at"] is not None
    assert _run(amb_status.remove(5001, by=ADMIN)) is False
    _seed(1)
    assert _run(amb_status.remove(1, by=ADMIN)) is False


# ── Швы одобрения и сверка ───────────────────────────────────────────────────────────────────

def test_on_approved_gives_slot_to_active_only(ready):
    _limit(17)
    _seed(1, status="pending")
    _seed(2, status="pending")
    _run(amb_status.request_join(1, source="button_bot"))
    assert _st(1)["slot_at"] is None
    _run(db.set_user_status(1, "approved"))
    _run(db.set_user_status(2, "approved"))
    _run(amb_status.on_applications_approved([1, 2, 424242]))
    assert _st(1)["slot_at"] is not None
    assert _st(2)["slot_at"] is None and _st(2)["status"] == "none"


def test_on_unapproved_releases_slot_without_pack(ready):
    _limit(2)
    _fill_slots(2)
    _run(sdb.set_pack(5001, True, at=AT))
    assert _run(amb_status.offer_open()) is False
    _run(db.set_user_status(5000, "pending"))
    _run(db.set_user_status(5001, "pending"))
    _run(amb_status.on_applications_unapproved([5000, 5001, 424242]))
    assert _st(5000)["slot_at"] is None and _st(5000)["status"] == "active"
    assert _st(5001)["slot_at"] is not None
    assert _run(amb_status.offer_open()) is True
    assert _run(amb_status.delegate_state(5000)) == "active_no_pack"


def test_on_unapproved_keeps_slot_if_still_approved(ready):
    _fill_slots(1)
    _run(amb_status.on_applications_unapproved([5000]))
    assert _st(5000)["slot_at"] is not None


def test_reconcile_slots(ready):
    _fill_slots(3)
    _run(db.set_user_status(5000, "rejected"))
    _run(db.set_user_status(5002, "pending"))
    assert _run(amb_status.reconcile_slots()) == 2
    assert _st(5001)["slot_at"] is not None
    assert _run(amb_status.reconcile_slots()) == 0


def test_seams_never_raise(ready, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sdb, "get_status", _boom)
    monkeypatch.setattr(sdb, "unapproved_slot_holders", _boom)
    monkeypatch.setattr(sdb, "release_slot", _boom)
    _fill_slots(1)
    _run(amb_status.on_applications_approved([5000]))
    _run(amb_status.on_applications_unapproved([5000]))
    assert _run(amb_status.reconcile_slots()) == 0


def test_tiers_failure_does_not_undo_join(ready, monkeypatch):
    async def _boom(tid):
        raise RuntimeError("tiers down")

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _boom)
    _seed(1)
    assert _run(amb_status.request_join(1, source="button_bot")).outcome == "active"
    assert _is_amb(1) == 1


# ── delegate_state ───────────────────────────────────────────────────────────────────────────

def test_delegate_state_values(ready):
    _limit(17)
    _fill_slots(1)
    _seed(1, status="pending")
    _seed(2)
    _seed(3)
    _seed(4)
    _run(sdb.set_status(1, "active", at=AT))
    _run(sdb.set_status(3, "declined", at=AT))
    _run(sdb.set_status(4, "candidate", at=AT))
    assert _run(amb_status.delegate_state(5000)) == "active_pack"
    assert _run(amb_status.delegate_state(1)) == "active_no_pack"
    assert _run(amb_status.delegate_state(2)) == "open"
    assert _run(amb_status.delegate_state(3)) == "declined"
    assert _run(amb_status.delegate_state(4)) == "open"
    _mode("selection")
    assert _run(amb_status.delegate_state(4)) == "candidate"


# ── Сервис aiogram-free ──────────────────────────────────────────────────────────────────────

def test_service_is_aiogram_free():
    tree = ast.parse((REPO / "services" / "amb_status.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(m == "aiogram" or m.startswith("aiogram.") for m in modules), modules
    assert not any(m.startswith("handlers") or m.startswith("miniapp") for m in modules), modules
