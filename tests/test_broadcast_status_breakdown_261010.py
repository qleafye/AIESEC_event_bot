"""Рассылка: разбивка получателей по статусу заявки и кнопка «✅ Только одобренные».

На проде рассылка «По фильтру → Чат делегатов → не в чате» без условия по статусу ушла
отклонённым и не подавшим анкету."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers import admin_broadcast_status as bst
from handlers import admin_broadcasts as ab
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, _fresh_state

NOT_SUBMITTED_ID = 999
ADMIN_ID = 1


def run(coro):
    return asyncio.run(coro)


def ready(tmp_path):
    config.DB_PATH = str(tmp_path / "bcast_status.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


async def add_user(tid, *, status="approved", city="msk"):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, event_city) VALUES (?, ?, ?, ?, ?)",
            (tid, f"Делегат {tid}", f"user{tid}", status, city),
        )
        await conn.commit()


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class _RecBot:
    def __init__(self):
        self.sent = []
        self.markups = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)


async def _seed():
    await add_user(11)
    await add_user(12)
    await add_user(21, status="pending")
    await add_user(31, status="rejected")


def test_filter_count_breakdown_and_only_approved(tmp_path):
    ready(tmp_path)
    run(_seed())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(filters=[{"field": "event_city", "value": "msk", "exclude": [], "label": "Москва"}]))
    cb = FakeCallback("filter_count", ADMIN_ID)
    run(ab.filter_count(cb, state))
    text = cb.message.text
    assert "Одобрены 2 · На рассмотрении 1 · Отклонены 1" in text
    assert "⚠️ В рассылке есть отклонённые (1) и не одобренные (1)" in text
    assert "bcstatus_filter" in _flat(cb.message.markup)

    cb2 = FakeCallback("bcstatus_filter", ADMIN_ID)
    run(bst.bcstatus_filter(cb2, state))
    filters = run(state.get_data())["filters"]
    assert {"field": "status", "value": "approved"} in filters
    assert "<b>2</b>" in cb2.message.text
    assert "⚠️" not in cb2.message.text
    assert "bcstatus_filter" not in _flat(cb2.message.markup)


def test_filter_with_status_no_warning(tmp_path):
    ready(tmp_path)
    run(_seed())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(filters=[{"field": "status", "value": "rejected"}]))
    cb = FakeCallback("filter_count", ADMIN_ID)
    run(ab.filter_count(cb, state))
    assert "Отклонены 1" in cb.message.text
    assert "⚠️ В рассылке" not in cb.message.text


def test_confirm_all_warns_and_trims_to_approved(tmp_path):
    ready(tmp_path)
    run(_seed())
    ids = [11, 12, 21, 31, NOT_SUBMITTED_ID]
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="all", bc_users=ids))
    bot = _RecBot()
    run(ab._send_confirm_prompt(bot, ADMIN_ID, state, len(ids), ids))
    text = bot.sent[-1][1]
    assert "Не подали анкету 1" in text
    assert "не одобренные (2)" in text
    flat = _flat(bot.markups[-1])
    assert flat.index("bcstatus_only") == flat.index("bc_go") + 1

    cb = FakeCallback("bcstatus_only", ADMIN_ID)
    run(bst.bcstatus_only(cb, state, bot))
    assert run(state.get_data())["bc_users"] == [11, 12]
    assert "Отправить это 2 пользователям" in bot.sent[-1][1]
    assert "⚠️ В рассылке" not in bot.sent[-1][1]


def test_confirm_filter_flow_adds_status_filter(tmp_path):
    ready(tmp_path)
    run(_seed())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="list", filters=[], bc_users=[11, 31]))
    bot = _RecBot()
    run(bst.bcstatus_only(FakeCallback("bcstatus_only", ADMIN_ID), state, bot))
    data = run(state.get_data())
    assert data["bc_users"] == [11]
    assert data["filters"] == [{"field": "status", "value": "approved"}]


def test_confirm_file_list_unchanged(tmp_path):
    ready(tmp_path)
    run(_seed())
    ids = [11, 31]
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(target_type="list", target_users=ids, bc_users=ids))
    bot = _RecBot()
    run(ab._send_confirm_prompt(bot, ADMIN_ID, state, len(ids), ids))
    assert "Получатели:" not in bot.sent[-1][1]
    assert "bcstatus_only" not in _flat(bot.markups[-1])


def test_only_approved_hidden_when_none_approved(tmp_path):
    ready(tmp_path)
    run(_seed())
    text, rows = run(bst.status_block([21, 31], [], "bcstatus_only"))
    assert "⚠️" in text and rows == []


def test_line_stays_after_only_approved(tmp_path):
    """После «Только одобренные» строка «Получатели: Одобрены N» остаётся — фильтр виден."""
    ready(tmp_path)
    run(_seed())
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(filters=[{"field": "event_city", "value": "msk", "exclude": [], "label": "Москва"}]))
    cb = FakeCallback("bcstatus_filter", ADMIN_ID)
    run(bst.bcstatus_filter(cb, state))
    assert "Получатели: Одобрены 2" in cb.message.text


def test_staff_note_splits_no_form_and_not_approved(tmp_path):
    ready(tmp_path)
    run(_seed())
    note = run(bst.staff_missing_note({21, NOT_SUBMITTED_ID}))
    assert "2 из команды (админы и менеджеры) рассылку не получат" in note
    assert "без анкеты делегата — 1" in note and "анкета не одобрена — 1" in note
    assert "не зарегистрированы" not in note
    only_no_form = run(bst.staff_missing_note({NOT_SUBMITTED_ID}))
    assert "не зарегистрированы как делегаты" in only_no_form
