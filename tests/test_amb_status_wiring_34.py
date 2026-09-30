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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Предложение ссылки после анкеты в боте и «Хочу свою ссылку»
# ══════════════════════════════════════════════════════════════════════════════════════════

def _fakes():
    from tests.test_skillup_referral_28 import _FakeCallback, _FakeMessage
    return _FakeCallback, _FakeMessage


def _schema_default(key):
    from settings_schema import SETTINGS_SCHEMA
    return SETTINGS_SCHEMA[key]["default"]


def _offer(tid):
    from handlers import reg_ambassador
    _cb, msg_cls = _fakes()
    msg = msg_cls(tid)
    _run(reg_ambassador.offer_ref_link(msg, tid))
    return msg


def _want(tid):
    from handlers import reg_ambassador
    cb_cls, _msg = _fakes()
    cb = cb_cls("regamb:want", tid)
    _run(reg_ambassador.regamb_want(cb))
    return cb


def test_offer_off_and_not_candidate_sends_nothing(ready):
    _seed(40, status="pending")
    assert not _offer(40).sent


def test_offer_off_selection_candidate_gets_ack_and_link(ready):
    """Так настроен РилТолк: вопрос в анкете включён, предложение ссылки — нет. Подтверждение
    обещает, что ссылка уже твоя, — она приходит следом (приглашения до вступления засчитываются)."""
    _mode("selection")
    _finalize(41, yes=True)
    msg = _offer(41)
    texts = [t for (t, _m, _p) in msg.sent]
    assert texts[0] == _schema_default("amb_candidate_ack_text")
    assert texts[1] == "https://t.me/TestBot?start=amb_41"
    assert msg.sent[1][2] is None  # голый URL без разметки
    assert len(texts) == 3  # + пояснение, где найти ссылку потом
    assert all(m is None for (_t, m, _p) in msg.sent)


def test_offer_on_selection_candidate_gets_ack_and_link_without_offer(ready):
    _run(db.set_setting("reg_offer_ref_link", "on"))
    _mode("selection")
    _finalize(42, yes=True)
    msg = _offer(42)
    texts = [t for (t, _m, _p) in msg.sent]
    assert texts[:2] == [_schema_default("amb_candidate_ack_text"), "https://t.me/TestBot?start=amb_42"]
    assert all(m is None for (_t, m, _p) in msg.sent)  # без кнопок «Хочу свою ссылку»
    assert _st(42)["status"] == "candidate"


def test_offer_candidate_without_bot_username_sends_only_ack(ready):
    from handlers import reg_ambassador
    from tests.test_skillup_referral_28 import _FakeBot, _FakeMessage
    _mode("selection")
    _finalize(46, yes=True)
    msg = _FakeMessage(46, bot=_FakeBot(fail=True))
    _run(reg_ambassador.offer_ref_link(msg, 46))
    assert [t for (t, _m, _p) in msg.sent] == [_schema_default("amb_candidate_ack_text")]


def test_offer_hidden_when_full_or_declined(ready):
    _run(db.set_setting("reg_offer_ref_link", "on"))
    _seed(43, status="pending")
    _seed(44, status="pending")
    _run(sdb.set_status(44, "declined", at=AT))
    assert not _offer(44).sent
    _limit(1)
    _fill_slots(1)
    assert not _offer(43).sent


def test_offer_on_open_shows_buttons(ready):
    _run(db.set_setting("reg_offer_ref_link", "on"))
    _seed(45, status="pending")
    msg = _offer(45)
    assert len(msg.sent) == 1
    datas = [b.callback_data for row in msg.sent[0][1].inline_keyboard for b in row]
    assert datas == ["regamb:want", "regamb:later"]


def test_want_instant_joins_and_sends_link(ready):
    _seed(50, status="pending")
    cb = _want(50)
    assert _st(50)["status"] == "active"
    texts = [t for (t, _m, _p) in cb.message.sent]
    assert texts[0] == "https://t.me/TestBot?start=amb_50"
    assert len(texts) == 2
    assert cb.answers == [(None, False)]
    assert ready == [50]  # ступени проверил сервис


def test_want_selection_candidate_gets_ack_and_link(ready):
    _mode("selection")
    _seed(51, status="pending")
    cb = _want(51)
    assert _st(51)["status"] == "candidate"
    user = _run(db.get_user(51))
    assert not user["is_ambassador"]
    texts = [t for (t, _m, _p) in cb.message.sent]
    assert texts[0] == _schema_default("amb_candidate_ack_text")
    assert texts[1] == "https://t.me/TestBot?start=amb_51"
    assert len(texts) == 3


@pytest.mark.parametrize("case", ["full", "declined"])
def test_want_full_or_declined_alerts_without_write(ready, case):
    _seed(52, status="pending")
    if case == "full":
        _limit(1)
        _fill_slots(1)
    else:
        _run(sdb.set_status(52, "declined", at=AT))
    before = _st(52)
    cb = _want(52)
    assert _st(52) == before
    assert not _run(db.get_user(52))["is_ambassador"]
    assert cb.answers == [(_schema_default("amb_slots_full_text"), True)]
    assert not cb.message.sent


def test_want_long_full_text_truncated_in_alert_and_sent_in_full(ready):
    long_text = "Места заняты. " * 30
    _run(db.set_setting("amb_slots_full_text", long_text))
    _seed(53, status="pending")
    _run(sdb.set_status(53, "declined", at=AT))
    cb = _want(53)
    (alert, show), = cb.answers
    assert show is True
    assert len(alert) <= 200 and alert.endswith("…")
    assert [t for (t, _m, _p) in cb.message.sent] == [long_text]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Место снимается, когда своя заявка амбассадора перестала быть одобренной
# ══════════════════════════════════════════════════════════════════════════════════════════

def _holder(tid, *, pack=False, city=None):
    _run(db.add_user({
        "telegram_id": tid, "full_name": f"Амбассадор {tid}",
        "registration_date": "2026-09-01 00:00:00", "season": SEASON,
        "event_city": city, "participant_type": "short" if city else None,
    }))
    _run(db.set_user_status(tid, "approved"))
    _run(sdb.set_status(tid, "active", at=AT))
    assert _run(sdb.try_claim_slot(tid, limit=0, season=SEASON, at=AT))
    if pack:
        _run(sdb.set_pack(tid, True, at=AT))


def test_revert_to_pending_releases_slot_without_pack(ready):
    from services.revert_pending import revert_to_pending
    _limit(1)
    _holder(60)
    assert _run(amb_status.offer_open()) is False
    report = _run(revert_to_pending(60, by_admin=1, notify=False))
    assert report["ok"]
    st = _st(60)
    assert st["slot_at"] is None and st["status"] == "active"
    assert _run(amb_status.offer_open()) is True


def test_revert_to_pending_keeps_slot_with_pack(ready):
    from services.revert_pending import revert_to_pending
    _holder(61, pack=True)
    assert _run(revert_to_pending(61, by_admin=1, notify=False))["ok"]
    assert _st(61)["slot_at"]


def test_city_move_to_moderation_releases_slot(ready, monkeypatch):
    from services.city_move import STATUS_MODE_TO_MODERATION, move_user_city
    from tests.test_city_move_260925 import _install_fake_sheets, _resolve_tabs
    store = _install_fake_sheets(monkeypatch)
    _run(db.set_setting("event_city_enabled", "on"))
    _holder(62, city="spb")
    store.seed(_run(_resolve_tabs("spb", "short")), [[62, "Амбассадор 62"]])
    store.seed(_run(_resolve_tabs("msk", "short")), [])
    report = _run(move_user_city(62, "msk", status_mode=STATUS_MODE_TO_MODERATION, by_admin=1))
    assert report["ok"] and report["status_changed"]
    assert _st(62)["slot_at"] is None


def test_edit_remoderation_releases_slot(ready):
    from services import reg_finalize as rf
    _run(db.set_setting("toggle_reg_edit_remoderation", "on"))
    _holder(63)
    result = _run(rf.finalize_data(63, "@d", {
        "telegram_id": 63, "kind": "edit", "answers": {"full_name": "Новое Имя"},
    }))
    assert result["status"] == "pending"
    assert _st(63)["slot_at"] is None


def test_edit_without_remoderation_keeps_slot(ready):
    from services import reg_finalize as rf
    _holder(64)
    _run(rf.finalize_data(64, "@d", {
        "telegram_id": 64, "kind": "edit", "answers": {"full_name": "Новое Имя"},
    }))
    assert _run(db.get_user(64))["status"] == "approved"
    assert _st(64)["slot_at"]


def test_resubmit_new_form_of_approved_releases_slot(ready):
    _run(db.set_setting("registration_mode", "full"))
    _run(db.set_setting("full_approval", "manual"))
    _holder(65)
    _finalize(65, yes=False)
    assert _run(db.get_user(65))["status"] == "pending"
    assert _st(65)["slot_at"] is None


def test_reject_decision_calls_unapproved(ready, monkeypatch):
    seen: list = []

    async def _spy(ids):
        seen.append(list(ids))

    monkeypatch.setattr(amb_status, "on_applications_unapproved", _spy)
    _seed(66, status="pending")
    assert _run(applications.claim_reject(66))
    _run(applications.record_decision(66, "rejected", "-", 1, datetime.now(),
                                      effects_already_sent=True))
    assert seen == [[66]]


def test_miniapp_reject_after_window_and_undo_call_unapproved(ready, monkeypatch):
    seen: list = []

    async def _spy(ids):
        seen.append(list(ids))

    monkeypatch.setattr(amb_status, "on_applications_unapproved", _spy)
    _seed(67, status="pending")
    _seed(68, status="pending")
    now = datetime.now()
    assert _run(applications.claim_web_decision(67, "rejected", None, 1, now)) is not None
    decision_id = _run(applications.claim_web_decision(68, "approved", None, 1, now))
    assert _run(applications.undo_decision(decision_id))["ok"]
    assert seen == [[68]]
    _run(applications.flush_due_decisions(
        now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1), lambda *_a: None,
    ))
    assert seen == [[68], [67]]
