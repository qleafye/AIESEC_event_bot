"""Ручное закрепление приглашённого за пригласившим (services/amb_journal.manual_attach)."""
from __future__ import annotations

import sqlite3

from config import config
from database import amb_journal_db, db
from services.amb import amb_journal
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


# ── экран и права ────────────────────────────────────────────────────────────────────────

def _flow_ready(tmp_path):
    from tests import test_amb_candidates_34 as c
    c._ready(tmp_path, "test_amb_attach_34.db")
    return c


def test_caps_resolve_for_every_attach_callback():
    from handlers.access.admin_caps import required_capability
    for data in ("admin_amb_attach", "ambj_pick:i:5", "ambj_go", "ambj_cancel"):
        assert required_capability(callback_data=data) == "moderate_game", data
    assert required_capability(raw_state="AmbAttach:waiting_note") == "moderate_game"


def test_section_has_attach_button():
    from handlers.settings.admin_sections import SECTIONS
    amb = next(s for s in SECTIONS if s[0] == "amb")
    assert ("screen", "admin_amb_attach", "📎 Закрепить приглашённого") in amb[2]


def test_screen_flow_pending_then_already_error(tmp_path):
    from handlers.amb import admin_amb_journal as h
    from tests.test_amb_bulk_34 import _cb, _person_msg
    from tests.test_amb_candidates_34 import _new_state, _seed
    c = _flow_ready(tmp_path)
    _seed(10, status="approved", name="Анна Смирнова", username="anna_s")
    _seed(20, status="pending", name="Иван Петров", username="ivan_p")
    state = _new_state()
    cb = _cb("admin_amb_attach")
    _run(h.attach_start(cb, state))
    assert "Кого привели?" in cb.message.answers[-1][0]

    msg = _person_msg("@ivan_p")
    _run(h.attach_invitee_step(msg, state))
    assert "Кто привёл?" in msg.answers[-1][0]
    msg = _person_msg("@anna_s")
    _run(h.attach_referrer_step(msg, state))
    assert "Откуда известно" in msg.answers[-1][0]
    msg = _person_msg("скрины в чате")
    _run(h.attach_note_step(msg, state))
    text = msg.answers[-1][0]
    assert "Иван Петров" in text and "Анна Смирнова" in text and "ждёт решения" in text

    cb = _cb("ambj_go")
    _run(h.attach_go(cb, state))
    assert "закреплён" in cb.message.answers[-1][0]
    assert _referrer_of(20) == 10

    # повтор: второй раз закрепить нельзя, ошибка называет прежнего пригласившего
    state = _new_state()
    _run(h.attach_start(_cb("admin_amb_attach"), state))
    msg = _person_msg("@ivan_p")
    _run(h.attach_invitee_step(msg, state))
    assert "уже числится за" in msg.answers[-1][0] and "Анна Смирнова" in msg.answers[-1][0]
    assert c  # модуль подготовки БД использован


def test_self_attach_and_command_exit(tmp_path):
    from handlers.amb import admin_amb_journal as h
    from tests.test_amb_bulk_34 import _cb, _person_msg
    from tests.test_amb_candidates_34 import _new_state, _seed
    _flow_ready(tmp_path)
    _seed(20, status="pending", name="Иван Петров", username="ivan_p")
    state = _new_state()
    _run(h.attach_start(_cb("admin_amb_attach"), state))
    _run(h.attach_invitee_step(_person_msg("@ivan_p"), state))
    msg = _person_msg("@ivan_p")
    _run(h.attach_referrer_step(msg, state))
    assert "за самим собой" in msg.answers[-1][0]
    msg = _person_msg("/start")
    _run(h.attach_referrer_step(msg, state))
    assert msg.answers[-1][0] == "Отменено."
    assert _run(state.get_state()) is None
