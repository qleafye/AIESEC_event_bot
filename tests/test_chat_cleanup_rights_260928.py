"""Автоочистка: «нет прав» ставится только по явной проверке прав бота.

Раньше одна ошибка «message can't be deleted» (конкретное сообщение, например старше 48 ч)
выключала очистку для всего чата, а вернуть её могла только сверка, которая работает лишь при
включённом учёте чата. Теперь ошибка про одно сообщение — проблема сообщения; ошибка,
похожая на нехватку прав, запускает getChatMember бота, и только его ответ ставит флаг.
Права бота перечитываются по расписанию и при выключенном учёте, если очистка включена.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from config import config
from database import db
from services import chat_cleanup, chat_tracking
from tests._dbtpl import fast_init_db

CHAT = -1009280201
BOT_ID = 777928201


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, types_="join"):
    config.DB_PATH = str(tmp_path / "rights.db")
    config.ADMIN_IDS = [1]
    fast_init_db()
    chat_cleanup._warned.clear()
    chat_cleanup._lanes.clear()
    _run(db.set_setting("delegate_chat_id", str(CHAT)))
    _run(db.set_setting("delegate_chat_title", "Делегаты"))
    if types_ is not None:
        _run(db.set_setting(chat_cleanup.TYPES_KEY, types_))


class _Bot:
    id = BOT_ID

    def __init__(self, error=None, member=None, member_error=None, admins=None):
        self.error = error
        self.member = member
        self.member_error = member_error
        self.admins = admins or []
        self.deleted = []
        self.member_calls = 0
        self.admin_calls = 0

    async def delete_message(self, chat_id, message_id):
        if self.error:
            raise Exception(self.error)
        self.deleted.append(message_id)
        return True

    async def get_chat_member(self, chat_id, user_id):
        self.member_calls += 1
        if self.member_error:
            raise Exception(self.member_error)
        return self.member

    async def get_chat_administrators(self, chat_id):
        self.admin_calls += 1
        return self.admins


def _own(status="administrator", can_delete=True):
    return SimpleNamespace(status=status, can_delete_messages=can_delete,
                           can_restrict_members=False,
                           user=SimpleNamespace(id=BOT_ID, is_bot=True))


def _can_delete():
    state = _run(db.get_chat_bot_state(CHAT))
    return None if state is None else state["can_delete"]


def test_single_undeletable_message_does_not_disable_chat(tmp_path):
    _ready(tmp_path)
    bot = _Bot(error="Bad Request: message can't be deleted", member=_own(can_delete=False))
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert _can_delete() != 0
    assert bot.member_calls == 0  # проблема сообщения, а не прав — проверять нечего
    bot.error = None
    _run(chat_cleanup.handle_service_message(bot, CHAT, 12, "join"))
    assert bot.deleted == [12]


def test_rights_error_with_rights_confirmed_keeps_cleanup_on(tmp_path):
    _ready(tmp_path)
    bot = _Bot(error="Bad Request: not enough rights to delete a message", member=_own(can_delete=True))
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.member_calls == 1
    assert _can_delete() == 1
    bot.error = None
    _run(chat_cleanup.handle_service_message(bot, CHAT, 12, "join"))
    assert bot.deleted == [12]


def test_rights_error_with_rights_missing_marks_chat_and_warns_once(tmp_path, caplog):
    _ready(tmp_path)
    bot = _Bot(error="Bad Request: CHAT_ADMIN_REQUIRED", member=_own(can_delete=False))
    with caplog.at_level(logging.WARNING, logger="services.chat_cleanup"):
        _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
        _run(chat_cleanup.handle_service_message(bot, CHAT, 12, "join"))
    assert _can_delete() == 0
    assert bot.member_calls == 1  # второе уведомление — без вызовов API вовсе
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_rights_check_failure_does_not_mark_chat(tmp_path):
    _ready(tmp_path)
    bot = _Bot(error="Bad Request: not enough rights", member_error="network is down")
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert _can_delete() != 0


def test_rights_refreshed_with_tracking_off_when_cleanup_enabled(tmp_path):
    _ready(tmp_path)
    _run(db.set_chat_bot_state(CHAT, "administrator", False))
    bot = _Bot(admins=[_own(can_delete=True)])
    reports = _run(chat_tracking.refresh_all_chats(bot))
    assert reports == []  # сверка состава выключена вместе с учётом
    assert bot.admin_calls == 1
    assert _can_delete() == 1


def test_no_rights_refresh_when_tracking_off_and_cleanup_off(tmp_path):
    _ready(tmp_path, types_=None)
    bot = _Bot(admins=[_own(can_delete=True)])
    _run(chat_tracking.refresh_all_chats(bot))
    assert bot.admin_calls == 0


def test_refresh_job_rechecks_rights_with_tracking_off(tmp_path, monkeypatch):
    import services.scheduler as sched

    _ready(tmp_path)
    _run(db.set_chat_bot_state(CHAT, "administrator", False))
    bot = _Bot(admins=[_own(can_delete=True)])
    monkeypatch.setattr(sched, "_bot", bot)
    _run(sched.chat_membership_refresh_job())
    assert bot.admin_calls == 1
    assert _can_delete() == 1
