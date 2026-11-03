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
