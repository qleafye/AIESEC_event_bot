"""Подключение правил входа в амбассадоры (`services/amb_status.py`) к поверхностям:
вопрос анкеты (общий движок бота и Mini App), ответ «да» → кандидат в режиме отбора, место в
лимите во всех путях одобрения, предложение ссылки после анкеты в боте и кнопка
«Хочу свою ссылку», снятие места, когда своя заявка перестала быть одобренной.

pytest-asyncio в окружении нет — async через `asyncio.run()`, временная БД — тот же приём,
что `tests/test_amb_status_service_34.py`.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import reg_engine
from config import config
from database import amb_status_db as sdb
from database import db
from services import amb_status, amb_tiers, applications
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
AT = "2026-09-30 12:00:00"
REPO = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "test_amb_status_wiring_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("reg_q_ambassador", "on"))
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    calls: list[int] = []

    async def _fake_tiers(tid):
        calls.append(int(tid))

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _fake_tiers)
    return calls


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


def _fill_slots(n, *, start=5000):
    for i in range(n):
        tid = start + i
        _seed(tid)
        _run(sdb.set_status(tid, "active", at=AT))
        assert _run(sdb.try_claim_slot(tid, limit=0, season=SEASON, at=AT))
    return [start + i for i in range(n)]


def _steps(data=None):
    return _run(reg_engine.enabled_steps(dict(data or {}), None))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Вопрос анкеты
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_question_hidden_when_limit_full_and_back_when_slot_freed(ready):
    _limit(2)
    holders = _fill_slots(2)
    assert "ambassador" not in _steps()
    _run(sdb.set_status(holders[0], "left", at=AT))  # без пакета — место освобождается
    assert "ambassador" in _steps()


def test_question_shown_without_limit_and_without_slot_count(ready, monkeypatch):
    async def _boom():
        raise AssertionError("COUNT мест не должен вызываться без лимита")

    monkeypatch.setattr(sdb, "slots_taken", _boom)
    assert "ambassador" in _steps()


def test_question_shown_when_gate_raises(ready, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("БД легла")

    monkeypatch.setattr(amb_status, "offer_open", _boom)
    assert "ambassador" in _steps()


def test_declined_sees_question_but_yes_is_noop(ready):
    _mode("selection")
    _limit(5)
    _seed(10, status="pending")
    _run(sdb.set_status(10, "declined", at=AT))
    assert "ambassador" in _steps()
    res = _run(amb_status.request_join(10, source="form"))
    assert res.outcome == "declined"
    assert _st(10)["status"] == "declined"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ответ «да» на финале анкеты
# ══════════════════════════════════════════════════════════════════════════════════════════

def _finalize(tid, *, yes, kind="new"):
    from services import reg_finalize as rf
    draft = {
        "telegram_id": tid, "kind": kind,
        "answers": {"full_name": f"Делегат {tid}", "is_ambassador_candidate": yes},
    }
    return _run(rf.finalize_data(tid, "@d", draft))


def test_selection_yes_makes_candidate_and_survives_resubmit_no(ready):
    _mode("selection")
    _finalize(20, yes=True)
    st = _st(20)
    assert st["status"] == "candidate"
    user = _run(db.get_user(20))
    assert not user["is_ambassador"]
    assert user["is_ambassador_candidate"]

    _finalize(20, yes=False)
    user = _run(db.get_user(20))
    assert not user["is_ambassador_candidate"]  # ответ анкеты перезаписан
    assert _st(20)["status"] == "candidate"  # статус кандидата — нет


def test_instant_yes_leaves_status_untouched(ready):
    _finalize(21, yes=True)
    assert _st(21)["status"] == "none"
    assert _run(db.get_user(21))["is_ambassador_candidate"]


def test_selection_no_answer_does_nothing(ready):
    _mode("selection")
    _finalize(22, yes=False)
    assert _st(22)["status"] == "none"


def test_selection_yes_when_full_does_not_make_candidate(ready):
    _mode("selection")
    _limit(1)
    _fill_slots(1)
    _finalize(23, yes=True)
    assert _st(23)["status"] == "none"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Место при одобрении своей заявки — все пути
# ══════════════════════════════════════════════════════════════════════════════════════════

def _active_pending(tid):
    _seed(tid, status="pending")
    _run(sdb.set_status(tid, "active", at=AT))
    assert _st(tid)["slot_at"] is None


def test_slot_on_bot_single_approval(ready):
    _limit(3)
    _active_pending(30)
    assert _run(applications.claim_approve(30))
    _run(applications.record_decision(30, "approved", None, 1, datetime.now(),
                                      effects_already_sent=True))
    assert _st(30)["slot_at"]


def test_slot_on_approve_all(ready):
    _limit(3)
    _active_pending(31)
    ids, _summary = _run(applications.claim_approve_all_with_credits(None))
    assert 31 in ids
    assert _st(31)["slot_at"]


def test_slot_on_miniapp_after_undo_window(ready):
    _limit(3)
    _active_pending(32)
    now = datetime.now()
    assert _run(applications.claim_web_decision(32, "approved", None, 1, now)) is not None
    assert _st(32)["slot_at"] is None  # окно отмены ещё идёт
    _run(applications.flush_due_decisions(
        now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1), lambda *_a: None,
    ))
    assert _st(32)["slot_at"]


def test_slot_on_auto_approval(ready):
    _limit(3)
    _active_pending(33)
    _run(db.set_setting("registration_mode", "full"))
    _run(db.set_setting("full_approval", "auto"))
    result = _finalize(33, yes=False)
    assert result["status"] == "approved"
    assert _st(33)["slot_at"]


def test_no_slot_on_approval_when_limit_full(ready):
    _limit(1)
    _fill_slots(1)
    _active_pending(34)
    assert _run(applications.claim_approve(34))
    _run(applications.record_decision(34, "approved", None, 1, datetime.now(),
                                      effects_already_sent=True))
    assert _st(34)["slot_at"] is None
    assert _st(34)["status"] == "active"


# ── AST-сторож: путь одобрения со ступенями обязан выдавать и место ─────────────────────────

def _call_name(node) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_APPROVAL_FILES = ("services/applications.py", "services/reg_finalize.py")


def _functions_calling(name: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel in _APPROVAL_FILES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = {_call_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
                if name in calls:
                    found[f"{rel}::{fn.name}"] = calls
    return found


def test_every_tier_check_site_also_gives_slot():
    """Каждый путь одобрения, который проверяет ступени приглашённых, обязан рядом звать
    `on_applications_approved` — иначе новый путь молча не выдаст место амбассадору."""
    sites = _functions_calling("check_tiers_for_invitees")
    assert len(sites) >= 4, sites  # сторож не пустой: record_decision, approve-all, flush, финал
    offenders = [k for k, calls in sites.items() if "on_applications_approved" not in calls]
    assert not offenders, "Одобрение без выдачи места амбассадору: " + ", ".join(offenders)
