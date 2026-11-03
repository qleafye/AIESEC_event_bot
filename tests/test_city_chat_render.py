"""Подстановка {chat} в момент отправки: URL из issue или токен исчезает."""
from __future__ import annotations

import asyncio

from services import chat_tracking, city_chat_link
from tests.test_city_chat_link import CHAT, Client, _bind, _ready

DELEGATE = 22001


def _run(coro):
    return asyncio.run(coro)


def _fresh(tmp_path):
    city_chat_link._fail_notified_at.clear()
    _ready(tmp_path, "city-render.db")


def test_no_token_no_probe(tmp_path):
    _fresh(tmp_path)

    async def go():
        await _bind()
        client = Client()
        text = await city_chat_link.render_for_delegate("Ждём вас на форуме.", DELEGATE, client)
        assert text == "Ждём вас на форуме."
        assert client.edits == [] and client.creates == []

    _run(go())


def test_token_uses_issue_url(tmp_path):
    _fresh(tmp_path)

    async def go():
        await _bind()
        client = Client()

        async def create(chat_id, name, expire_date, member_limit, creates_join_request):
            invite = await Client.create_invite(
                client, chat_id, name, expire_date, member_limit, creates_join_request,
            )
            invite.invite_link = "https://t.me/+a&b=1"
            return invite

        client.create_invite = create
        text = await city_chat_link.render_for_delegate("Ссылка: {chat}", DELEGATE, client)
        assert text == "Ссылка: https://t.me/+a&amp;b=1"
        assert client.creates and client.creates[0][0] == CHAT

    _run(go())


def test_failure_drops_token_not_old_url(tmp_path):
    _fresh(tmp_path)

    async def go():
        await _bind()
        await city_chat_link.city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+secretold", creates_join_request=1,
        )
        client = Client()
        client.edit_exc = Exception("chat not found")
        text = await city_chat_link.render_for_delegate(
            "Добро пожаловать\nЧат города: {chat}", DELEGATE, client,
        )
        assert "{chat}" not in text
        assert "secretold" not in text
        assert "Чат города:" not in text
        assert "Добро пожаловать" in text
        assert client.creates == []

    _run(go())


def test_failure_notifies_once(tmp_path):
    _fresh(tmp_path)
    sent = []

    async def notify(city, phrase):
        sent.append((city, phrase))

    async def go():
        await _bind()
        await city_chat_link.city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+secretold", creates_join_request=1,
        )
        client = Client()
        client.edit_exc = Exception("429")
        first = await city_chat_link.render_for_delegate(
            "Чат города: {chat}", DELEGATE, client, notify=notify,
        )
        second = await city_chat_link.render_for_delegate(
            "ещё {chat}", DELEGATE, client, notify=notify,
        )
        assert first == ""
        assert "{chat}" not in second and "secretold" not in second
        assert len(sent) == 1
        assert "Не смог проверить ссылку" in sent[0][1]
        assert "t.me" not in sent[0][1]
        assert "delegate_chat_id" not in sent[0][1]
        assert "creates_join_request" not in sent[0][1]

    _run(go())


def test_manager_phrase_has_no_codes(tmp_path):
    _fresh(tmp_path)
    sent = []

    async def notify(city, phrase):
        sent.append(phrase)

    async def go():
        await chat_tracking.bind_chat(1, CHAT, "Делегаты", None)
        client = Client()
        client.edit_exc = Exception("CHAT_ADMIN_REQUIRED")
        await city_chat_link.city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+secretold", creates_join_request=1,
        )
        await city_chat_link.render_for_delegate("{chat}", DELEGATE, client, notify=notify)
        assert sent
        assert "delegate_chat_id" not in sent[0]
        assert "creates_join_request" not in sent[0]
        assert "приглашать" in sent[0]

    _run(go())


def test_approve_appends_token_once(tmp_path, monkeypatch):
    """Письмо без токена получает одну фразу. Токен менеджера второй фразой не обрастает."""
    from database import db
    from handlers.reg import reg_schema

    seen = []

    async def grab(text, telegram_id, client, *, notify=None):
        seen.append(text)
        return text

    monkeypatch.setattr(city_chat_link, "render_for_delegate", grab)
    _fresh(tmp_path)

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            return None

    async def go():
        await db.set_setting("approve_text", "Добро пожаловать на форум.")
        await reg_schema.send_completion_and_bonus(Bot(), DELEGATE, with_menu=False)
        await db.set_setting("approve_text", "Ссылка уже есть: {chat}")
        await reg_schema.send_completion_and_bonus(Bot(), DELEGATE, with_menu=False)

    _run(go())
    assert seen[0].count("Чат города: {chat}") == 1
    assert seen[0].count("{chat}") == 1
    assert seen[0].startswith("Добро пожаловать на форум.")
    assert seen[1].count("Чат города: {chat}") == 0
    assert seen[1].count("{chat}") == 1
    assert seen[1].startswith("Ссылка уже есть: {chat}")


def test_quiet_queue_keeps_token(tmp_path, monkeypatch):
    """Очередь хранит {chat}. Утренний разбор сам спрашивает ссылку и шлёт уже URL."""
    from datetime import datetime

    from database import db
    from services import scheduler as sched
    from services.comms import quiet_hours as qh

    issue_calls = []

    async def fake_issue(client, city):
        issue_calls.append(city)
        return city_chat_link.LinkResult(True, "https://t.me/+live", None, True)

    monkeypatch.setattr(city_chat_link, "issue", fake_issue)
    _fresh(tmp_path)

    class Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.messages.append(text)

    bot = Bot()
    sched._bot = bot
    now = datetime(2026, 9, 16, 23, 30)
    due = datetime(2026, 9, 17, 9, 0)

    async def go():
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "00:00")
        await db.set_setting("quiet_hours_end", "23:59")

        async def sender():
            raise AssertionError("в тихие часы отправлять нельзя")

        queued = await qh.send_or_queue_text(
            now, DELEGATE, "Ждём в чате: {chat}", sender=sender,
        )
        assert queued is False
        assert issue_calls == []
        rows = await db.list_due_delayed_notifications(due.strftime("%Y-%m-%d %H:%M:%S"))
        assert rows and "{chat}" in rows[0]["payload"]["text"]
        assert "https://t.me" not in rows[0]["payload"]["text"]
        await qh.flush_due(due)
        assert bot.messages
        assert "https://t.me/+live" in bot.messages[-1]
        assert "{chat}" not in bot.messages[-1]
        assert issue_calls

    try:
        _run(go())
    finally:
        sched._bot = None


def test_copy_message_untouched(tmp_path, monkeypatch):
    """Копия без нашей подписи не проходит render. Текстовая рассылка — на каждого получателя."""
    from handlers.comms import admin_broadcasts
    from handlers.states import Broadcast
    from tests.test_broadcast_important_260924 import (
        FakeBot, FakeCallback, FakeSentMessage, FakeState, _fast_sleep,
    )

    calls = []

    async def grab(text, telegram_id, client, *, notify=None):
        calls.append(telegram_id)
        return text

    monkeypatch.setattr(city_chat_link, "render_for_delegate", grab)
    _fresh(tmp_path)
    _fast_sleep(monkeypatch)

    async def _run_bc(state):
        await state.set_state(Broadcast.confirm)
        confirm = FakeSentMessage(1, "Отправить?")
        cb = FakeCallback("bc_go", user_id=1, message=confirm)
        bot = FakeBot()
        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]
        return bot

    async def go():
        copy_state = FakeState(
            bc_users=[1, 2], bc_preview="фото", bc_kind="media",
            bc_content_html="подпись", bc_chat_id=9, bc_message_id=8, bc_important=False,
        )
        copy_bot = await _run_bc(copy_state)
        assert calls == []
        assert copy_bot.copy_calls == [1, 2]

        text_state = FakeState(
            bc_users=[1, 2], bc_preview="новость", bc_kind="text",
            bc_content_html="Новость без ссылки", bc_important=False,
        )
        await _run_bc(text_state)
        assert calls == [1, 2]

    _run(go())
