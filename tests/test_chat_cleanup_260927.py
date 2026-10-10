"""Автоочистка служебных уведомлений в чатах делегатов (services/chat_cleanup.py).

Бот удаляет только отмеченные менеджером типы служебных уведомлений («вступил(а)»,
«вышел(а)», «закрепил(а)»…) и только в привязанных чатах делегатов. Без права «Удаление
сообщений» — одно предупреждение на чат, дальше тишина без вызовов API, пока права не
вернут. pytest-asyncio нет — `asyncio.run()`; БД — `fast_init_db`.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

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
    # Флаг «нет прав» ставит только явная проверка прав бота (getChatMember), а не сама ошибка.
    _ready(tmp_path)
    bot = _Bot(error="Telegram server says - Bad Request: not enough rights to delete a message")
    bot.id = 777927

    async def _member(chat_id, user_id):
        return SimpleNamespace(status="member")

    bot.get_chat_member = _member
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


def test_delay_queues_in_db_not_in_jobstore(tmp_path, monkeypatch):
    # Отложенное удаление — строка в chat_cleanup_queue, а не date-джоба на каждое уведомление.
    _ready(tmp_path)
    _run(db.set_setting(chat_cleanup.DELAY_KEY, "30"))
    jobs = []

    class _Sched:
        def add_job(self, *a, **kw):
            jobs.append((a, kw))

    import services.scheduler as sched
    monkeypatch.setattr(sched, "get_scheduler", lambda: _Sched())
    bot = _Bot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 11, "join"))
    assert bot.deleted == []
    assert jobs == []

    async def _queued():
        async with db._connect() as conn:
            async with conn.execute("SELECT chat_id, message_id, code FROM chat_cleanup_queue") as cur:
                return await cur.fetchall()
    assert _run(_queued()) == [(CHAT, 11, "join")]


def test_delay_queue_failure_deletes_immediately(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting(chat_cleanup.DELAY_KEY, "30"))

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(chat_cleanup, "enqueue_chat_cleanup", _boom)
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
    from domain.settings.schema import SETTINGS_SCHEMA, _parse_setting, multi_options
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


# ── Задача 2: групповые хендлеры — учёт ДО удаления, состояние бота ─────────────────────

from datetime import datetime  # noqa: E402

from aiogram.types import (  # noqa: E402
    Chat, ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner, ChatMemberUpdated,
    Message, User,
)

from handlers import group_chat  # noqa: E402
from services import chat_tracking  # noqa: E402

BOT_ID = 777927
PERSON = 900927499


class _OrderBot(_Bot):
    """Запоминает, что было в chat_events в момент удаления — учёт обязан идти раньше."""

    def __init__(self):
        super().__init__()
        self.id = BOT_ID
        self.events_at_delete = None

    async def delete_message(self, chat_id, message_id):
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT event FROM chat_events WHERE chat_id = ? AND telegram_id = ?",
                (chat_id, PERSON),
            ) as cur:
                self.events_at_delete = [r[0] for r in await cur.fetchall()]
        return await super().delete_message(chat_id, message_id)


def _group_msg(**extra):
    return Message(
        message_id=55, date=datetime.now(), chat=Chat(id=CHAT, type="supergroup", title="Делегаты"),
        from_user=User(id=PERSON, is_bot=False, first_name="Оля"), **extra,
    )


def test_join_notice_tracked_before_delete(tmp_path):
    _ready(tmp_path, types_="join")
    bot = _OrderBot()
    msg = _group_msg(new_chat_members=[User(id=PERSON, is_bot=False, first_name="Оля")])
    _run(group_chat.on_new_chat_members(msg, bot))
    assert bot.deleted == [(CHAT, 55)]
    assert bot.events_at_delete == ["join"]
    assert _run(db.chat_member_row(CHAT, PERSON))["status"] == "member"


def test_leave_notice_tracked_before_delete(tmp_path):
    _ready(tmp_path, types_="leave")
    _run(db.upsert_chat_member(CHAT, PERSON, "member", source="chat_member"))
    bot = _OrderBot()
    msg = _group_msg(left_chat_member=User(id=PERSON, is_bot=False, first_name="Оля"))
    _run(group_chat.on_left_chat_member(msg, bot))
    assert bot.deleted == [(CHAT, 55)]
    assert bot.events_at_delete == ["leave"]


def _handler(name):
    return next(h for h in group_chat.router.message.handlers if h.callback.__name__ == name)


def test_pin_notice_goes_to_service_handler_not_activity(tmp_path):
    _ready(tmp_path, types_="pin")
    _run(db.set_setting("chat_tracking_enabled", "on"))
    pinned = Message(message_id=10, date=datetime.now(),
                     chat=Chat(id=CHAT, type="supergroup"))
    msg = _group_msg(pinned_message=pinned)
    handlers = [h.callback.__name__ for h in group_chat.router.message.handlers]
    assert handlers.index("on_group_service_message") < handlers.index("on_group_message")
    ok, _kw = _run(_handler("on_group_service_message").check(msg))
    assert ok
    bot = _OrderBot()
    _run(group_chat.on_group_service_message(msg, bot))
    assert bot.deleted == [(CHAT, 55)]

    async def _activity():
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM chat_activity") as cur:
                return (await cur.fetchone())[0]
    assert _run(_activity()) == 0


def test_topic_created_is_not_a_service_handler_match(tmp_path):
    _ready(tmp_path)
    from aiogram.types import ForumTopicCreated
    msg = _group_msg(forum_topic_created=ForumTopicCreated(name="Флуд", icon_color=0))
    ok, _kw = _run(_handler("on_group_service_message").check(msg))
    assert not ok


def _admin(user, can_delete):
    known = dict(
        status="administrator", user=user, can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=can_delete, can_manage_video_chats=True,
        can_restrict_members=True, can_promote_members=False, can_change_info=True,
        can_invite_users=True, can_post_stories=False, can_edit_stories=False,
        can_delete_stories=False,
    )
    for name, field in ChatMemberAdministrator.model_fields.items():
        if field.is_required() and name not in known:
            known[name] = False
    return ChatMemberAdministrator(**known)


def _bot_update(new_member):
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Бот")
    return ChatMemberUpdated(
        chat=Chat(id=CHAT, type="supergroup", title="Делегаты"),
        from_user=User(id=PERSON, is_bot=False, first_name="Оля"), date=datetime.now(),
        old_chat_member=ChatMemberMember(status="member", user=bot_user),
        new_chat_member=new_member(bot_user),
    )


def test_bot_membership_records_delete_right(tmp_path):
    _ready(tmp_path)

    class _B:
        id = BOT_ID

        async def send_message(self, *a, **k):
            return None

    cases = [
        (lambda u: _admin(u, False), "administrator", 0),
        (lambda u: _admin(u, True), "administrator", 1),
        (lambda u: ChatMemberMember(status="member", user=u), "member", 0),
        (lambda u: ChatMemberOwner(status="creator", user=u, is_anonymous=False), "creator", 1),
    ]
    for build, status, flag in cases:
        _run(group_chat.on_bot_membership_changed(_bot_update(build), _B()))
        state = _run(db.get_chat_bot_state(CHAT))
        assert (state["bot_status"], state["can_delete"]) == (status, flag), status


def _own_admin(can_delete):
    return SimpleNamespace(status="administrator", can_delete_messages=can_delete,
                           user=SimpleNamespace(id=BOT_ID, is_bot=True))


def test_refresh_reads_own_admin_entry_and_is_fail_soft(tmp_path):
    # План 04 свёл перечитывание прав бота к одному вызову getChatAdministrators
    # (chat_tracking.refresh_chat_admins) — тот же ответ даёт и админов группы.
    _ready(tmp_path)

    class _B:
        id = BOT_ID

        async def get_chat_administrators(self, chat_id):
            return [_own_admin(True)]

    _run(chat_tracking.refresh_chat_admins(_B(), CHAT))
    assert _run(db.get_chat_bot_state(CHAT))["can_delete"] == 1

    class _Broken:
        id = BOT_ID

        async def get_chat_administrators(self, chat_id):
            raise RuntimeError("network")

    _run(chat_tracking.refresh_chat_admins(_Broken(), CHAT))  # не бросает
    assert _run(db.get_chat_bot_state(CHAT))["can_delete"] == 1


def test_refresh_all_chats_refreshes_bot_state(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("chat_tracking_enabled", "on"))

    class _B:
        id = BOT_ID

        async def get_chat_administrators(self, chat_id):
            return [_own_admin(False)]

    _run(chat_tracking.refresh_all_chats(_B()))
    assert _run(db.get_chat_bot_state(CHAT))["can_delete"] == 0


# ── Задача 3: экран «🧹 Служебные сообщения в чате» ──────────────────────────────────────

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from handlers import admin_chat_cleanup as scr  # noqa: E402
from handlers.admin_caps import required_capability  # noqa: E402
from handlers.states import ChatCleanupEdit  # noqa: E402


class _U:
    id = ADMIN


class _M:
    def __init__(self, text=None):
        self.text = text
        self.from_user = _U()
        self.answers = []
        self.edited = None
        self.markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)
        self.markup = reply_markup

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text
        self.markup = reply_markup


class _CB:
    def __init__(self, data):
        self.data = data
        self.from_user = _U()
        self.message = _M()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _st():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_screen_lists_types_and_toggles_codes(tmp_path):
    _ready(tmp_path, types_=None)
    text, kb = _run(scr.render_chat_cleanup_screen(ADMIN))
    for label in chat_cleanup.CLEANUP_TYPES.values():
        assert any(label in t for t in _texts(kb))
    assert "SOS" in text
    assert "ничего не удаляется" in text
    for code in chat_cleanup.CLEANUP_TYPES:
        assert code not in text + "".join(_texts(kb))
    join_cb = [cd for cd, t in zip(_cbs(kb), _texts(kb)) if chat_cleanup.CLEANUP_TYPES["join"] in t][0]
    cb = _CB(join_cb)
    _run(scr.chclean_toggle(cb))
    assert _run(chat_cleanup.ticked_codes()) == ["join"]
    assert any(t.startswith("✅") and chat_cleanup.CLEANUP_TYPES["join"] in t for t in _texts(cb.message.markup))
    _run(scr.chclean_toggle(_CB(join_cb)))
    assert _run(chat_cleanup.ticked_codes()) == []
    forged = _CB("chclean:t:nope")
    _run(scr.chclean_toggle(forged))
    assert forged.answers and forged.answers[0][1]


def test_chat_status_lines(tmp_path):
    _ready(tmp_path)
    text, _ = _run(scr.render_chat_cleanup_screen(ADMIN))
    assert "❔ «Делегаты»: права ещё не проверялись" in text
    _run(db.set_chat_bot_state(CHAT, "administrator", False))
    text, _ = _run(scr.render_chat_cleanup_screen(ADMIN))
    assert "⚠️ «Делегаты»: сделайте бота администратором с правом «Удаление сообщений»" in text
    _run(db.set_chat_bot_state(CHAT, "administrator", True))
    text, _ = _run(scr.render_chat_cleanup_screen(ADMIN))
    assert "✅ «Делегаты»: бот удаляет" in text
    _run(db.delete_setting("delegate_chat_id"))
    text, _ = _run(scr.render_chat_cleanup_screen(ADMIN))
    assert "Чат делегатов ещё не подключён" in text


def test_delay_is_typed_int(tmp_path):
    _ready(tmp_path)
    _, kb = _run(scr.render_chat_cleanup_screen(ADMIN))
    assert any(t.startswith("⏱ Удалять через: сразу") for t in _texts(kb))
    state = _st()
    _run(scr.chclean_delay(_CB("chclean:delay"), state))
    assert _run(state.get_state()) == ChatCleanupEdit.waiting_for_value.state
    bad = _M("-5")
    _run(scr.chclean_delay_value(bad, state))
    assert _run(db.get_setting(chat_cleanup.DELAY_KEY)) is None
    assert bad.answers
    good = _M("60")
    _run(scr.chclean_delay_value(good, state))
    assert _run(db.get_setting(chat_cleanup.DELAY_KEY)) == "60"
    assert _run(state.get_state()) is None
    assert any(t.startswith("⏱ Удалять через: 60 сек") for t in _texts(good.markup))


def test_cleanup_capabilities():
    assert required_capability(callback_data="admin_chat_cleanup") == "settings"
    assert required_capability(callback_data="chclean:t:join") == "settings"
    assert required_capability(raw_state="ChatCleanupEdit:waiting_for_value") == "settings"


def test_cleanup_screen_row_in_manage_section():
    from handlers import admin_sections as sec
    callbacks = [sec.row_callback(r) for r in sec._declared_rows("manage")]
    assert callbacks.index("admin_chat_cleanup") == callbacks.index("admin_chat_rating") + 1
