"""Сегмент рассылки по статусу заявки, включая лист ожидания.

Тумблер выключен — пикер статуса по-прежнему берёт значения из базы.
Тумблер включён — четыре кнопки, даже если таких заявок ноль.
Текст письма в код не зашит: менеджер пишет его в обычной рассылке.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from database import db
from database.app_status_db import split_ids_by_app_status
from handlers.comms import admin_broadcast_status as bst
from handlers.comms import admin_broadcasts as ab
from handlers.states import Broadcast
from tests._enroll38 import ADMIN_ID, add_user, ready, run
from tests.test_roles_phase8 import FakeCallback, _fresh_state

_BUTTONS = ("Новая", "Одобрена", "Отклонена", "Лист ожидания")
_CODES = ("pending", "approved", "rejected", "waitlist")


def _opt_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row if b.callback_data.startswith("filter_opt:")]


def _open_status_picker(state):
    run(state.set_state(Broadcast.filter_field))
    run(state.update_data(filters=[], filter_pending_field="status"))
    cb = FakeCallback("filter_f_status", ADMIN_ID)
    run(ab._show_value_picker(cb, state, "status", "Выберите значение"))
    return cb


def test_split_does_not_fold_waitlist_into_pending(tmp_path):
    ready(tmp_path)
    run(add_user(1, status="approved"))
    run(add_user(2, status="pending"))
    run(add_user(3, status="rejected"))
    run(add_user(4, status="waitlist"))
    run(add_user(5, status=""))
    run(add_user(8, status="legacy"))
    run(add_user(9, status="waitlist"))
    groups = run(split_ids_by_app_status([9, 1, 2, 4, 3, 5, 6, 8]))
    assert groups["waitlist"] == [9, 4]
    assert groups["approved"] == [1]
    assert groups["pending"] == [2, 5, 8]
    assert groups["rejected"] == [3]
    assert groups["not_submitted"] == [6]
    assert 4 not in groups["pending"] and 4 not in groups["rejected"]
    assert 9 not in groups["pending"]


def test_status_line_names_waitlist(tmp_path):
    ready(tmp_path)
    run(add_user(1, status="approved"))
    run(add_user(2, status="pending"))
    run(add_user(4, status="waitlist"))
    text, rows = run(bst.status_block([1, 2, 4], [], "bcstatus_only"))
    assert "Лист ожидания 1" in text
    assert "На рассмотрении 1" in text
    assert "в листе ожидания (1)" in text
    assert "не одобренные (1)" in text
    assert "отказ" not in text.lower()
    state = _fresh_state(ADMIN_ID)
    run(state.set_state(Broadcast.confirm))
    run(state.update_data(target_type="all", bc_users=[1, 4]))

    class _Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.sent.append(text)

    run(bst.bcstatus_only(FakeCallback("bcstatus_only", ADMIN_ID), state, _Bot()))
    assert run(state.get_data())["bc_users"] == [1]


def test_line_without_waitlist_skips_empty_basket(tmp_path):
    ready(tmp_path)
    run(add_user(1, status="approved"))
    run(add_user(2, status="pending"))
    text, _rows = run(bst.status_block([1, 2], [], "bcstatus_only"))
    assert text.startswith("Получатели: Одобрены 1 · На рассмотрении 1\n")
    assert "Лист ожидания" not in text


def test_picker_four_buttons_when_toggle_on_even_if_empty(tmp_path, monkeypatch):
    ready(tmp_path)
    run(db.set_setting("apps_waitlist_enabled", "on"))
    called = []

    async def fake_distinct(field):
        called.append(field)
        return []

    monkeypatch.setattr(ab, "get_distinct_filter_values", fake_distinct)
    state = _fresh_state(ADMIN_ID)
    cb = _open_status_picker(state)
    buttons = _opt_buttons(cb.message.markup)
    assert [b.text for b in buttons] == list(_BUTTONS)
    assert [b.callback_data for b in buttons] == [f"filter_opt:{i}" for i in range(4)]
    assert called == []
    joined = " ".join(b.callback_data for b in buttons)
    for code in _CODES:
        assert code not in joined
    run(ab.filter_pick_value(FakeCallback("filter_opt:3", ADMIN_ID), state))
    filters = run(state.get_data())["filters"]
    assert filters[-1] == {"field": "status", "value": "waitlist"}
    _open_status_picker(state)
    run(ab.filter_pick_value(FakeCallback("filter_opt:0", ADMIN_ID), state))
    assert run(state.get_data())["filters"][-1] == {"field": "status", "value": "pending"}


def test_picker_unchanged_when_toggle_off(tmp_path, monkeypatch):
    ready(tmp_path)
    called = []

    async def fake_distinct(field):
        called.append(field)
        return ["approved", "pending"]

    monkeypatch.setattr(ab, "get_distinct_filter_values", fake_distinct)
    state = _fresh_state(ADMIN_ID)
    cb = _open_status_picker(state)
    buttons = _opt_buttons(cb.message.markup)
    assert called == ["status"]
    assert {b.text for b in buttons} == {"Одобрена", "Новая"}
    assert "Лист ожидания" not in {b.text for b in buttons}
    assert len(buttons) != 4


def test_no_hardcoded_campaign():
    src = inspect.getsource(bst.status_segment_options)
    low = src.lower()
    assert "ваш статус" not in low
    assert "одобрен" not in low
    assert "отказ" not in low
    for company in ("яндекс", "сбер", "google", "тинькофф"):
        assert company not in low
    warning = Path("handlers/comms/admin_broadcast_status.py").read_text(encoding="utf-8")
    share = warning.split("в листе ожидания", 1)[1][:80].lower()
    assert "отказ" not in share
    assert "ваш статус" not in warning.lower()
