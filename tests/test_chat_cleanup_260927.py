"""Автоочистка служебных уведомлений в чатах делегатов (services/chat_cleanup.py).

Бот удаляет только отмеченные менеджером типы служебных уведомлений («вступил(а)»,
«вышел(а)», «закрепил(а)»…) и только в привязанных чатах делегатов. Без права «Удаление
сообщений» — одно предупреждение на чат, дальше тишина без вызовов API, пока права не
вернут. pytest-asyncio нет — `asyncio.run()`; БД — `fast_init_db`.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.enums import ContentType

from config import config
from database import db
from services import chat_cleanup
from tests._dbtpl import fast_init_db

ADMIN = 900927401
CHAT = -1009270001
SOS_CHAT = -1009270002


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, types_="join"):
    config.DB_PATH = str(tmp_path / "cleanup.db")
    config.ADMIN_IDS = [ADMIN]
    fast_init_db()
    chat_cleanup._warned.clear()
    _run(db.set_setting("delegate_chat_id", str(CHAT)))
    _run(db.set_setting("delegate_chat_title", "Делегаты"))
    _run(db.set_setting("sos_chat_id", str(SOS_CHAT)))
    if types_ is not None:
        _run(db.set_setting(chat_cleanup.TYPES_KEY, types_))


class _Bot:
    def __init__(self, error: str | None = None):
        self.deleted = []
        self.error = error

    async def delete_message(self, chat_id, message_id):
        if self.error:
            raise Exception(self.error)
        self.deleted.append((chat_id, message_id))
        return True


def test_ticked_type_in_bound_chat_is_deleted(tmp_path):
    _ready(tmp_path)
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == [(CHAT, 11)]


def test_nothing_is_deleted_by_default(tmp_path):
    _ready(tmp_path, types_=None)
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == []


def test_unticked_type_stays(tmp_path):
    _ready(tmp_path, types_="leave")
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == []


def test_sos_and_foreign_chats_are_never_touched(tmp_path):
    _ready(tmp_path, types_="join\nleave\npin")
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, SOS_CHAT, 11, "join"))
    _run(chat_cleanup.handle_service_message(bot, -100555, 12, "join"))
    assert bot.deleted == []


def test_no_rights_warns_once_and_stops_calling_api(tmp_path, caplog):
    _ready(tmp_path)
    bot = _Bot(error="Telegram server says - Bad Request: message can't be deleted")
    with caplog.at_level(logging.WARNING, logger="services.chat_cleanup"):
        _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
        state = _run(db.get_chat_bot_state(CHAT))
        assert state["can_delete"] == 0
        bot.error = None
        _run(chat_cleanup.handle_service_message(bot, CHAT, 12, "join"))
    assert bot.deleted == []  # второе уведомление — без вызова API
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_rights_granted_again_resumes_cleanup(tmp_path):
    _ready(tmp_path)
    _run(db.set_chat_bot_state(CHAT, "administrator", False))
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == []
    _run(db.set_chat_bot_state(CHAT, "administrator", True))
    _run(chat_cleanup.handle_service_message(bot, CHAT, 12, "join"))
    assert bot.deleted == [(CHAT, 12)]


def test_already_deleted_message_is_silent(tmp_path, caplog):
    _ready(tmp_path)
    bot = _Bot(error="Bad Request: message to delete not found")
    with caplog.at_level(logging.INFO, logger="services.chat_cleanup"):
        _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    state = _run(db.get_chat_bot_state(CHAT))
    assert state is None or state["can_delete"] != 0


def test_delay_schedules_persistent_job(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting(chat_cleanup.DELAY_KEY, "30"))
    jobs = []

    class _Sched:
        def add_job(self, func, trigger, **kw):
            jobs.append((func, trigger, kw))

    import services.scheduler as sched
    monkeypatch.setattr(sched, "get_scheduler", lambda: _Sched())
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == []
    func, trigger, kw = jobs[0]
    assert func is chat_cleanup.delete_service_message_job
    assert trigger == "date"
    assert kw["id"] == f"chatclean_{CHAT}_11"
    assert kw["args"] == [CHAT, 11]


def test_delay_without_scheduler_deletes_immediately(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting(chat_cleanup.DELAY_KEY, "30"))

    def _boom():
        raise RuntimeError("Scheduler not initialised")

    import services.scheduler as sched
    monkeypatch.setattr(sched, "get_scheduler", _boom)
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == [(CHAT, 11)]


def test_delete_job_uses_scheduler_bot(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _Bot()
    import services.scheduler as sched
    monkeypatch.setattr(sched, "get_bot", lambda: bot)
    _run(chat_cleanup.delete_service_message_job(CHAT, 11))
    assert bot.deleted == [(CHAT, 11)]


def test_content_type_map_is_closed_and_keeps_topic_root():
    known = {c.value for c in ContentType}
    assert set(chat_cleanup.CONTENT_TYPE_TO_CODE) <= known
    assert "forum_topic_created" not in chat_cleanup.CONTENT_TYPE_TO_CODE
    assert set(chat_cleanup.CONTENT_TYPE_TO_CODE.values()) <= set(chat_cleanup.CLEANUP_TYPES)
    assert chat_cleanup.CONTENT_TYPE_TO_CODE["new_chat_members"] == "join"
    assert chat_cleanup.CONTENT_TYPE_TO_CODE["left_chat_member"] == "leave"


def test_registry_multi_uses_cleanup_types():
    from settings_schema import SETTINGS_SCHEMA, _parse_setting, multi_options
    entry = SETTINGS_SCHEMA[chat_cleanup.TYPES_KEY]
    assert entry["type"] == "multi" and entry["group"] == "chat"
    assert [c for c, _ in multi_options(chat_cleanup.TYPES_KEY)] == list(chat_cleanup.CLEANUP_TYPES)
    assert _parse_setting(chat_cleanup.TYPES_KEY, None) == []
    assert SETTINGS_SCHEMA[chat_cleanup.DELAY_KEY]["default"] == 0


def test_chat_bot_state_idempotent_and_purge_excluded(tmp_path):
    _ready(tmp_path)
    _run(db.set_chat_bot_state(CHAT, "administrator", True))
    _run(db.init_db())
    state = _run(db.get_chat_bot_state(CHAT))
    assert state["bot_status"] == "administrator" and state["can_delete"] == 1
    assert "chat_bot_state" in db.USER_PURGE_EXCLUDED
    _run(db.set_chat_bot_state(CHAT, None, False))
    state = _run(db.get_chat_bot_state(CHAT))
    assert state["bot_status"] == "administrator"  # None = статус не меняем
    assert state["can_delete"] == 0
