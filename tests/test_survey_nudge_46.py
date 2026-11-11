"""Догонялка несдатчиков анкеты: не всем approved, только пришедшие сегодня без сдачи."""
from __future__ import annotations

import asyncio
from datetime import datetime

import domain.cities as cities
from config import config
from database import db, survey_db
from handlers.access.admin_caps import required_capability
from handlers.comms import admin_survey_results  # noqa: F401 — регистрирует psv_nudge*
from services.comms import survey_nudge
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db
from tests.test_polls_260822 import ADMIN_ID, _cb, _state
from tests.test_roles_phase8 import _flat_callback_data
from tests.test_surveys_flow_261024 import FakeBot, U1, U2, _add_user, _survey

U3 = 920013
U4 = 920014
BOUND_MSK = 920021

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "nudge.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _arrive(tid: int, day: str | None = None):
    day = day or msk_now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (?, 'entry', ?, 'scan', ?, ?)",
            (tid, now, now, day),
        )
        await conn.commit()


async def _city(tid: int, code: str):
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET event_city = ? WHERE telegram_id = ?", (code, tid))
        await conn.commit()


def test_nudge_ids_arrived_unfinished_only(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        await _add_user(U2)
        await _add_user(U3)
        pid, qids = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await db.record_poll_message(pid, U2, None, 2, True)
        await db.record_poll_message(pid, U3, None, 3, True)
        await survey_db.start_survey_progress(pid, U1, qids[0])
        await survey_db.finish_survey(pid, U1, qids[0])
        await _arrive(U1)
        await _arrive(U2)
        return await survey_nudge.nudge_ids(pid)

    assert _run(go()) == [U2]


def test_nudge_skips_approved_who_did_not_arrive_or_get_invite(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        await _add_user(U2)
        await _add_user(U3)
        pid, _ = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        await _arrive(U3)
        return await survey_nudge.nudge_ids(pid)

    # U1 — доставили и пришёл; U2 approved, приглашение не уходило; U3 пришёл, но не получал.
    assert _run(go()) == [U1]


def test_nudge_ids_city_scope(tmp_path):
    _ready(tmp_path)
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    try:
        async def go():
            await _add_user(U1)
            await _add_user(U2)
            await _city(U1, "msk")
            await _city(U2, "spb")
            pid, _ = await _survey(status="open")
            await db.record_poll_message(pid, U1, None, 1, True)
            await db.record_poll_message(pid, U2, None, 2, True)
            await _arrive(U1)
            await _arrive(U2)
            msk = await survey_nudge.nudge_ids(pid, city_scope=cities.city_scope("msk"))
            all_cities = await survey_nudge.nudge_ids(pid)
            return msk, all_cities

        msk, all_cities = _run(go())
        assert msk == [U1]
        assert all_cities == [U1, U2]
    finally:
        cities.set_cities_for_test(saved)


def test_send_nudge_skips_who_finished_in_between(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, qids = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        ids = await survey_nudge.nudge_ids(pid)
        await survey_db.start_survey_progress(pid, U1, qids[0])
        await survey_db.finish_survey(pid, U1, qids[0])
        bot = FakeBot()
        poll = await db.get_poll(pid)
        return ids, await survey_nudge.send_nudge(bot, poll, ids), bot.messages

    ids, (sent, failed), messages = _run(go())
    assert ids == [U1]
    assert (sent, failed) == (0, 0)
    assert messages == []


def test_send_nudge_delivers_invite(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        bot = FakeBot()
        poll = await db.get_poll(pid)
        ids = await survey_nudge.nudge_ids(pid)
        counts = await survey_nudge.send_nudge(bot, poll, ids)
        return pid, ids, counts, bot.messages

    pid, ids, (sent, failed), messages = _run(go())
    assert ids == [U1]
    assert (sent, failed) == (1, 0)
    assert messages[0][0] == U1
    assert messages[0][2].inline_keyboard[0][0].callback_data == f"sv:s:{pid}"


def test_card_shows_nudge_only_when_someone_to_remind(tmp_path):
    from handlers.comms.admin_survey_results import render_survey_card

    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey(status="open")
        empty, kb_empty = await render_survey_card(await db.get_poll(pid), admin_id=ADMIN_ID)
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        filled, kb_filled = await render_survey_card(await db.get_poll(pid), admin_id=ADMIN_ID)
        return pid, _flat_callback_data(kb_empty), _flat_callback_data(kb_filled)

    pid, empty, filled = _run(go())
    assert f"psv_nudge:{pid}" not in empty
    assert f"psv_nudge:{pid}" in filled


def test_ask_and_go_send_same_invite(tmp_path):
    from tests.test_polls_260822 import FakeBot as PollBot

    _ready(tmp_path)

    async def seed():
        await _add_user(U1)
        pid, _ = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        return pid

    pid = _run(seed())
    state, bot = _state(ADMIN_ID), PollBot()
    _, ev = _cb(f"psv_nudge:{pid}", state, bot)
    assert "Напомнить 1 чел." in ev.message.text
    assert f"psv_nudge_go:{pid}" in _flat_callback_data(ev.message.markup)
    _, ev = _cb(f"psv_nudge_go:{pid}", state, bot)
    assert "Напомнил 1." in ev.message.text
    assert bot.sent_messages and bot.sent_messages[0][0] == U1


def test_nudge_callbacks_require_broadcast():
    assert required_capability(callback_data="psv_nudge:1") == "broadcast"
    assert required_capability(callback_data="psv_nudge_go:1") == "broadcast"


def test_second_send_is_silent(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        bot = FakeBot()
        poll = await db.get_poll(pid)
        ids = await survey_nudge.nudge_ids(pid)
        first = await survey_nudge.send_nudge(bot, poll, ids)
        second = await survey_nudge.send_nudge(bot, poll, ids)
        return first, second, bot.messages, await db.get_user(U1)

    first, second, messages, user = _run(go())
    assert first == (1, 0)
    assert second == (0, 0)
    assert len(messages) == 1
    assert user["status"] == "approved"


def test_failed_send_is_not_marked_so_retry_works(tmp_path):
    _ready(tmp_path)

    class Once:
        def __init__(self):
            self.calls = 0
            self.messages = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("сеть")
            self.messages.append(chat_id)

            class Msg:
                message_id = 1

            return Msg()

    async def go():
        await _add_user(U1)
        pid, _ = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        bot = Once()
        poll = await db.get_poll(pid)
        ids = await survey_nudge.nudge_ids(pid)
        first = await survey_nudge.send_nudge(bot, poll, ids)
        second = await survey_nudge.send_nudge(bot, poll, ids)
        return first, second, bot.messages

    first, second, messages = _run(go())
    assert first == (0, 1)
    assert second == (1, 0)
    assert messages == [U1]


def test_screen_people_match_header_list(tmp_path):
    _ready(tmp_path)
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    try:
        async def go():
            await db.set_setting("event_city_enabled", "on")
            assert await cities.set_admin_city(ADMIN_ID, "msk") is True
            await _add_user(U1)
            await _add_user(U2)
            await _city(U1, "msk")
            await _city(U2, "spb")
            pid, _ = await _survey(status="open")
            await db.record_poll_message(pid, U1, None, 1, True)
            await db.record_poll_message(pid, U2, None, 2, True)
            await _arrive(U1)
            await _arrive(U2)
            view = await survey_nudge.screen(ADMIN_ID)
            scoped = await survey_nudge.nudge_ids(pid, city_scope=cities.city_scope("msk"))
            return view, scoped

        view, scoped = _run(go())
    finally:
        cities.set_cities_for_test(saved)
    assert scoped == [U1]
    assert len(view["surveys"]) == 1
    assert [person["id"] for person in view["surveys"][0]["people"]] == scoped
    assert view["surveys"][0]["count"] == 1
    assert "одобрен" in view["text"].lower()
    assert "forbidden" not in view["text"].lower()


def test_preview_does_not_send_and_queue_sends_once(tmp_path):
    from services.infra.miniapp_outbox import _handle_row

    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey(status="open")
        await db.record_poll_message(pid, U1, None, 1, True)
        await _arrive(U1)
        preview = await survey_nudge.invite_decision(ADMIN_ID, pid, confirm=False)
        marked = await survey_db.survey_nudged_user_ids(pid)
        ready = await survey_nudge.invite_decision(ADMIN_ID, pid, confirm=True)
        bot = FakeBot()
        await _handle_row(bot, "survey_nudge", ready["enqueue"])
        after = len(bot.messages)
        await _handle_row(bot, "survey_nudge", ready["enqueue"])
        again = await survey_nudge.invite_decision(ADMIN_ID, pid, confirm=True)
        status = (await db.get_user(U1))["status"]
        return preview, marked, ready, after, len(bot.messages), again, status

    preview, marked, ready, after, total, again, status = _run(go())
    assert preview["need_confirm"] is True
    assert "enqueue" not in preview
    assert "второй раз" in preview["text"].lower()
    assert marked == set()
    assert ready["enqueue"] == {"admin_id": ADMIN_ID, "poll_id": ready["enqueue"]["poll_id"]}
    assert after == 1
    assert total == 1
    assert again["ok"] is False
    assert again["text"] == survey_nudge.ALREADY
    assert status == "approved"


def test_chat_button_stays_and_site_only_links():
    from pathlib import Path

    from handlers.comms.admin_survey_results import survey_nudge_ask, survey_nudge_go
    from dashboard.forum_day import NUDGE_HREF, nudge_page

    assert survey_nudge_ask is not None
    assert survey_nudge_go is not None
    assert required_capability(callback_data="psv_nudge:1") == "broadcast"
    assert required_capability(callback_data="psv_nudge_go:1") == "broadcast"
    assert "forbidden" not in survey_nudge.NEED_CAP.lower()
    assert "broadcast" not in survey_nudge.NEED_CAP
    page = nudge_page()
    assert NUDGE_HREF == "/app/#/nudge"
    assert NUDGE_HREF in page
    assert "SELECT" not in page
    assert "nudge_ids" not in page
    source = Path("dashboard/forum_day.py").read_text(encoding="utf-8")
    assert "nudge_ids" not in source
    assert "SELECT" not in source
