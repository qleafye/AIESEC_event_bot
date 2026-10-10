"""Рассылка и сезон: текущий сезон по умолчанию в «По фильтру», «из них прошлого сезона» на
экранах подсчёта и подтверждений, кнопка «Только текущий сезон»."""
from __future__ import annotations

import json
from datetime import datetime

from database import db
from database.db import SEASON_CURRENT
from handlers.comms import admin_broadcast_season as bs
from handlers.comms import admin_broadcasts as ab
from handlers.states import Broadcast
from tests._enroll38 import ADMIN_ID, add_user, ready, run, seed_delegates
from tests.test_roles_phase8 import FakeCallback, FakeMessage, _fresh_state


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


class _RecBot:
    def __init__(self):
        self.sent = []
        self.markups = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)


def _confirm(state, bot, ids):
    run(ab._send_confirm_prompt(bot, ADMIN_ID, state, len(ids), ids))


def test_filter_start_prefills_current_season(tmp_path):
    ready(tmp_path)
    run(seed_delegates())
    state = _fresh_state(ADMIN_ID)
    cb = FakeCallback("broadcast_filter", ADMIN_ID)
    run(ab.broadcast_filter_start(cb, state))
    filters = run(state.get_data())["filters"]
    assert len(filters) == 1 and filters[0]["field"] == "season"
    assert filters[0]["value"] == SEASON_CURRENT and "YL 26/2" in filters[0]["label"]
    assert "bcseason_all" in _flat(cb.message.markup)


def test_filter_start_no_prefill_single_season(tmp_path):
    ready(tmp_path)
    run(db.set_setting("event_season", "YL 26/2"))
    run(add_user(1))
    state = _fresh_state(ADMIN_ID)
    run(ab.broadcast_filter_start(FakeCallback("broadcast_filter", ADMIN_ID), state))
    assert run(state.get_data())["filters"] == []


def test_filter_start_no_prefill_without_event_season(tmp_path):
    ready(tmp_path)
    run(add_user(1))
    run(add_user(2, season="YL 26/1"))
    state = _fresh_state(ADMIN_ID)
    run(ab.broadcast_filter_start(FakeCallback("broadcast_filter", ADMIN_ID), state))
    assert run(state.get_data())["filters"] == []


def test_include_past_button_removes_condition(tmp_path):
    ready(tmp_path)
    run(seed_delegates())
    state = _fresh_state(ADMIN_ID)
    run(ab.broadcast_filter_start(FakeCallback("broadcast_filter", ADMIN_ID), state))
    cb = FakeCallback("bcseason_all", ADMIN_ID)
    run(bs.bcseason_all(cb, state))
    assert run(state.get_data())["filters"] == []
    assert "bcseason_cur" in _flat(cb.message.markup)
    assert "🎯 Только текущий сезон" in _texts(cb.message.markup)
    cb2 = FakeCallback("bcseason_cur", ADMIN_ID)
    run(bs.bcseason_cur(cb2, state))
    assert run(state.get_data())["filters"][0]["value"] == SEASON_CURRENT
    assert "bcseason_all" in _flat(cb2.message.markup)


def test_filter_count_past_note(tmp_path):
    ready(tmp_path)
    run(seed_delegates())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(filters=[{"field": "status", "value": "approved"}]))
    run(state.set_state(Broadcast.filter_field))
    cb = FakeCallback("filter_count", ADMIN_ID)
    run(ab.filter_count(cb, state))
    assert "из них прошлого сезона: 1" in cb.message.text
    run(state.update_data(filters=[
        {"field": "status", "value": "approved"},
        {"field": "season", "value": SEASON_CURRENT, "label": "Текущий"},
    ]))
    cb2 = FakeCallback("filter_count", ADMIN_ID)
    run(ab.filter_count(cb2, state))
    assert "прошлого сезона" not in cb2.message.text


def test_past_season_note_pure(tmp_path):
    from services import broadcast_scope as sc
    ready(tmp_path)
    ids = run(seed_delegates())
    allids = [ids["cur1"], ids["empty"], ids["past"]]
    assert run(sc.past_season_note(allids)) == "\nиз них прошлого сезона: 1"
    assert run(sc.past_season_note([ids["cur1"]])) == ""
    assert run(sc.current_season_only(allids)) == [ids["cur1"], ids["empty"]]


# ── подтверждения ───────────────────────────────────────────────────────────────────────────

def test_all_confirm_shows_past_and_button(tmp_path):
    ready(tmp_path)
    ids = run(seed_delegates())
    users = [ids["cur1"], ids["past"]]
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="all", bc_users=users))
    bot = _RecBot()
    _confirm(state, bot, users)
    assert "из них прошлого сезона: 1" in bot.sent[-1][1]
    assert "bcseason_only" in _flat(bot.markups[-1])


def test_bcseason_only_narrows_immediate(tmp_path):
    ready(tmp_path)
    ids = run(seed_delegates())
    users = [ids["cur1"], ids["empty"], ids["past"]]
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="all", bc_users=users))
    run(state.set_state(Broadcast.confirm))
    bot = _RecBot()
    run(bs.bcseason_only(FakeCallback("bcseason_only", ADMIN_ID), state, bot))
    assert run(state.get_data())["bc_users"] == [ids["cur1"], ids["empty"]]
    assert "прошлого сезона" not in bot.sent[-1][1]
    assert "bcseason_only" not in _flat(bot.markups[-1])
    assert "2 пользователям" in bot.sent[-1][1]


def test_incomplete_path_same(tmp_path):
    ready(tmp_path)
    ids = run(seed_delegates())
    users = [ids["past"], 777]  # 777 — только начал анкету, в users его нет
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="list", target_users=users, bc_users=users))
    bot = _RecBot()
    _confirm(state, bot, users)
    assert "из них прошлого сезона: 1" in bot.sent[-1][1]
    assert "bcseason_only" in _flat(bot.markups[-1])


def test_filter_path_not_nagged(tmp_path):
    ready(tmp_path)
    ids = run(seed_delegates())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="list", filters=[], bc_users=[ids["past"]]))
    bot = _RecBot()
    _confirm(state, bot, [ids["past"]])
    assert "прошлого сезона" not in bot.sent[-1][1]
    assert "bcseason_only" not in _flat(bot.markups[-1])


def test_schedule_all_only_current(tmp_path, monkeypatch):
    ready(tmp_path)
    run(seed_delegates())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(schedule_dt=datetime(2030, 1, 1, 12, 0), sched_text="hi", sched_photo=None))
    msg = FakeMessage()
    run(ab._send_schedule_confirm_prompt(msg, state))
    text, _pm, kb = msg.answers[-1]
    assert "прошлого сезона: 1" in text
    assert "bcseason_sched" in _flat(kb)

    cb = FakeCallback("bcseason_sched", ADMIN_ID)
    run(bs.bcseason_sched(cb, state))
    assert "bcseason_sched" not in _flat(cb.message.answers[-1][2])

    created = {}

    async def fake_create(text, photo, spec, when, admin, important=False):
        created["spec"] = spec
        return 1

    monkeypatch.setattr(ab, "create_scheduled_broadcast", fake_create)
    monkeypatch.setattr(ab, "schedule_broadcast_job", lambda *a, **k: None)
    run(ab.sched_go(FakeCallback("sched_go", ADMIN_ID), state))
    spec = json.loads(created["spec"])
    assert spec[0]["value"] == SEASON_CURRENT


def test_no_past_no_noise(tmp_path):
    ready(tmp_path)
    run(db.set_setting("event_season", "YL 26/2"))
    run(add_user(1))
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="all", bc_users=[1]))
    bot = _RecBot()
    _confirm(state, bot, [1])
    assert bot.sent[-1][1] == "Отправить это 1 пользователям?"
    assert "bcseason_only" not in _flat(bot.markups[-1])


def test_picking_concrete_season_replaces_current_sentinel(tmp_path):
    ready(tmp_path)
    run(seed_delegates())
    state = _fresh_state(ADMIN_ID)
    run(ab.broadcast_filter_start(FakeCallback("broadcast_filter", ADMIN_ID), state))
    run(state.update_data(filter_pending_field="season", filter_options=["YL 26/1"]))
    run(ab.filter_pick_value(FakeCallback("filter_opt:0", ADMIN_ID), state))
    filters = run(state.get_data())["filters"]
    assert [(f["field"], f["value"]) for f in filters] == [("season", "YL 26/1")]
    assert run(db.count_and_list_filtered(filters)) == [105]
