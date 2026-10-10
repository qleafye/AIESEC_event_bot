"""Запись состояния бота в группе (chat_bot_state) не обрывает привязку чата.

Ошибка БД на этой строке (например, locked сверх таймаута) раньше рвала хендлер
my_chat_member до привязки чата и до личного сообщения админу.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram.types import Chat, ChatMemberMember, ChatMemberOwner, ChatMemberUpdated, User

from config import config
from database import db
from handlers.chat import group_chat
from services import chat_tracking
from tests._dbtpl import fast_init_db

ADMIN = 900928301
CHAT = -1009283001
BOT_ID = 777928301


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    id = BOT_ID

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(chat_id)


def test_bot_state_write_failure_does_not_block_binding(tmp_path, monkeypatch, caplog):
    config.DB_PATH = str(tmp_path / "failsoft.db")
    config.ADMIN_IDS = [ADMIN]
    fast_init_db()

    async def _locked(*a, **k):
        raise RuntimeError("database is locked")

    async def _no_reconcile(*a, **k):
        return None

    monkeypatch.setattr(group_chat, "set_chat_bot_state", _locked)
    monkeypatch.setattr(chat_tracking, "schedule_bind_reconcile", _no_reconcile)
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Бот")
    event = ChatMemberUpdated(
        chat=Chat(id=CHAT, type="supergroup", title="Делегаты"),
        from_user=User(id=ADMIN, is_bot=False, first_name="Админ"), date=datetime.now(),
        old_chat_member=ChatMemberMember(status="member", user=bot_user),
        new_chat_member=ChatMemberOwner(status="creator", user=bot_user, is_anonymous=False),
    )
    # creator не ветка привязки — проверяем, что хендлер вообще дошёл до конца без исключения
    _run(group_chat.on_bot_membership_changed(event, _Bot()))

    from tests.test_chat_cleanup_260927 import _admin
    event = event.model_copy(update={"new_chat_member": _admin(bot_user, True)})
    bot = _Bot()
    with caplog.at_level(logging.WARNING, logger="handlers.chat.group_chat"):
        _run(group_chat.on_bot_membership_changed(event, bot))
    assert _run(db.get_setting("delegate_chat_id")) == str(CHAT)
    assert ADMIN in bot.sent
    assert any("chat_bot_state" in r.getMessage() for r in caplog.records)
