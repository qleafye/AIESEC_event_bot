"""Квик 260914-rgr (RGR-01..07), задача 1: таблицы чата, привязка группы, учёт участников,
групповой роутер.

Правка 15.09 (владелец, «привязка через личку админа»): бот больше НИКОГДА не пишет в саму
группу — ни подтверждение привязки, ни вопрос о городе. `on_bot_membership_changed` пишет
ЛИЧНО промоутеру (`event.from_user`), с фолбэком на `config.ADMIN_IDS`, если личка не
доставилась; выбор города (`chatbind:{code}:{chat_id}`) — коллбэк из ЛИЧНОГО чата, живёт в
`group_chat.private_router`, не в `group_chat.router`.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path (тот же
приём, что в tests/test_polls_260822.py::_ready).
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Chat, ChatMemberAdministrator, ChatMemberBanned, ChatMemberLeft, ChatMemberMember,
    ChatMemberUpdated, Message, Update, User,
)

import cities
from config import config
from database import db
from handlers import group_chat
from services import chat_tracking

ADMIN_ID = 900701
SECOND_ADMIN_ID = 900702
STRANGER_ID = 900704
CHAT_ID = -1001234567890
BOT_ID = 777000


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "chat.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())


@contextmanager
def _cities_on(tmp_path):
    """Сеет реестр городов, включает модуль, гарантированно откатывает CITIES после теста
    (та же дисциплина, что tests/test_city_admin_phase71.py::test_city_toggle_spb_...)."""
    _ready(tmp_path)
    saved = list(cities.CITIES)
    try:
        asyncio.run(cities.seed_cities_if_empty())
        asyncio.run(cities.reload_cities())
        asyncio.run(db.set_setting("event_city_enabled", "on"))
        yield
    finally:
        cities.set_cities_for_test(saved)


def _admin_status(user: User) -> ChatMemberAdministrator:
    """Собирает `ChatMemberAdministrator` независимо от версии aiogram: код бота читает
    только `.status`/`.user`, поэтому любые новые обязательные булевы разрешений (например,
    `can_send_welcome_messages`, добавленный в 3.31) достаточно проставить в False —
    без этого конструктор Pydantic падает `ValidationError` на каждой новой версии API."""
    known = dict(
        status="administrator", user=user, can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
        can_restrict_members=True, can_promote_members=True, can_change_info=True,
        can_invite_users=True, can_post_stories=False, can_edit_stories=False,
        can_delete_stories=False,
    )
    for name, field in ChatMemberAdministrator.model_fields.items():
        if field.is_required() and name not in known:
            known[name] = False
    return ChatMemberAdministrator(**known)


def _member_status(user: User) -> ChatMemberMember:
    return ChatMemberMember(status="member", user=user)


def _left_status(user: User) -> ChatMemberLeft:
    return ChatMemberLeft(status="left", user=user)


def _kicked_status(user: User) -> ChatMemberBanned:
    return ChatMemberBanned(status="kicked", user=user, until_date=datetime.now())


class FakeBot:
    def __init__(self, bot_id=BOT_ID, fail_dm_ids=(), chat_title="Делегаты"):
        self.id = bot_id
        self.sent: list[tuple] = []
        self.fail_dm_ids = set(fail_dm_ids)
        self.chat_title = chat_title

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        if chat_id in self.fail_dm_ids:
            raise RuntimeError("промоутер ни разу не открывал бота")
        self.sent.append((chat_id, text, reply_markup))

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, title=self.chat_title)


class _FakeChat:
    def __init__(self, chat_id, title="Делегаты"):
        self.id = chat_id
        self.title = title


class _FakeCbUser:
    def __init__(self, uid):
        self.id = uid


class _FakeCbMessage:
    def __init__(self, chat):
        self.chat = chat
        self.edited: list[str] = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited.append(text)


class FakeCallback:
    def __init__(self, data, user_id, chat):
        self.data = data
        self.from_user = _FakeCbUser(user_id)
        self.message = _FakeCbMessage(chat)
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeGroupMessage:
    """Дублирует поля, которые читает `group_chat.on_group_message` — намеренно несёт
    `.text`, который хендлер НИКОГДА не обязан прочитать (проверяется отдельным тестом
    схемы таблиц ниже)."""

    def __init__(self, user_id, chat_id=CHAT_ID, reply_to_message=None, media_attr=None,
                 text="секретный текст делегата, которого в БД быть не должно"):
        self.from_user = User(id=user_id, is_bot=False, first_name="Делегат")
        self.chat = Chat(id=chat_id, type="supergroup")
        self.reply_to_message = reply_to_message
        self.text = text
        for attr in group_chat._MEDIA_ATTRS:
            setattr(self, attr, None)
        if media_attr:
            setattr(self, media_attr, object())


async def _events_for(chat_id, telegram_id):
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT event FROM chat_events WHERE chat_id = ? AND telegram_id = ? ORDER BY id",
            (chat_id, telegram_id),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def _table_columns(table):
    async with db._connect() as conn:
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
    return {row[1] for row in rows}


# ── Привязка через my_chat_member: ЛИЧКА промоутеру, НИКОГДА группа ──────────────────────

def test_bind_cities_off_dms_promoter_and_never_posts_to_group(tmp_path):
    _ready(tmp_path)
    bot = FakeBot()
    bot_user = User(id=bot.id, is_bot=True, first_name="Bot")
    admin_user = User(id=ADMIN_ID, is_bot=False, first_name="Менеджер")
    event = ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup", title="Делегаты"),
        from_user=admin_user, date=datetime.now(),
        old_chat_member=_member_status(bot_user), new_chat_member=_admin_status(bot_user),
    )

    asyncio.run(group_chat.on_bot_membership_changed(event, bot))

    chats = asyncio.run(chat_tracking.bound_chats())
    assert len(chats) == 1
    assert chats[0]["chat_id"] == CHAT_ID
    assert isinstance(chats[0]["chat_id"], int)
    assert not any(chat_id == CHAT_ID for chat_id, _t, _kb in bot.sent)  # НИКОГДА в группу
    assert any(chat_id == ADMIN_ID and "Делегаты" in text for chat_id, text, _kb in bot.sent)


def test_bind_cities_on_asks_promoter_privately_with_city_buttons(tmp_path):
    with _cities_on(tmp_path):
        bot = FakeBot()
        bot_user = User(id=bot.id, is_bot=True, first_name="Bot")
        admin_user = User(id=ADMIN_ID, is_bot=False, first_name="Менеджер")
        event = ChatMemberUpdated(
            chat=Chat(id=CHAT_ID, type="supergroup", title="Делегаты"),
            from_user=admin_user, date=datetime.now(),
            old_chat_member=_member_status(bot_user), new_chat_member=_admin_status(bot_user),
        )

        asyncio.run(group_chat.on_bot_membership_changed(event, bot))

        assert asyncio.run(chat_tracking.bound_chats()) == []  # город ещё не выбран
        assert not any(chat_id == CHAT_ID for chat_id, _t, _kb in bot.sent)
        dm = next(s for s in bot.sent if s[0] == ADMIN_ID)
        assert "К какому городу относится" in dm[1]
        flat = [btn.callback_data for row in dm[2].inline_keyboard for btn in row]
        assert any(cd.startswith("chatbind:") and cd.endswith(f":{CHAT_ID}") for cd in flat)


def test_bind_falls_back_to_admin_ids_when_promoter_dm_fails(tmp_path):
    _ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID, SECOND_ADMIN_ID]
    bot = FakeBot(fail_dm_ids={ADMIN_ID})
    bot_user = User(id=bot.id, is_bot=True, first_name="Bot")
    promoter = User(id=ADMIN_ID, is_bot=False, first_name="Менеджер")
    event = ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup", title="Делегаты"),
        from_user=promoter, date=datetime.now(),
        old_chat_member=_member_status(bot_user), new_chat_member=_admin_status(bot_user),
    )

    asyncio.run(group_chat.on_bot_membership_changed(event, bot))

    assert not any(chat_id == CHAT_ID for chat_id, _t, _kb in bot.sent)  # группа молчит
    assert not any(chat_id == ADMIN_ID for chat_id, _t, _kb in bot.sent)  # личка промоутеру упала
    assert any(chat_id == SECOND_ADMIN_ID for chat_id, _t, _kb in bot.sent)  # фолбэк дошёл


def test_bind_rejected_when_promoter_is_not_bot_admin_user(tmp_path):
    _ready(tmp_path)
    bot = FakeBot()
    bot_user = User(id=bot.id, is_bot=True, first_name="Bot")
    stranger = User(id=STRANGER_ID, is_bot=False, first_name="Стажёр")
    event = ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup", title="Делегаты"),
        from_user=stranger, date=datetime.now(),
        old_chat_member=_member_status(bot_user), new_chat_member=_admin_status(bot_user),
    )

    asyncio.run(group_chat.on_bot_membership_changed(event, bot))

    assert asyncio.run(chat_tracking.bound_chats()) == []
    assert bot.sent == []  # D-1: ни личка, ни группа — ничего вообще


def test_bot_left_chat_unbinds_and_alerts_admins(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))
    bot = FakeBot()
    bot_user = User(id=bot.id, is_bot=True, first_name="Bot")
    event = ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup", title="Делегаты"),
        from_user=User(id=STRANGER_ID, is_bot=False, first_name="Кто-то"), date=datetime.now(),
        old_chat_member=_admin_status(bot_user), new_chat_member=_left_status(bot_user),
    )

    asyncio.run(group_chat.on_bot_membership_changed(event, bot))

    assert asyncio.run(chat_tracking.bound_chats()) == []
    assert any(ADMIN_ID == chat_id for chat_id, _text, _kb in bot.sent)


# ── Выбор города по chatbind: — коллбэк ИЗ ЛИЧНОГО чата (group_chat.private_router) ──────

def test_chatbind_pick_writes_per_city_key_not_global(tmp_path):
    with _cities_on(tmp_path):
        bot = FakeBot()
        cb = FakeCallback(f"chatbind:spb:{CHAT_ID}", ADMIN_ID, _FakeChat(ADMIN_ID))

        asyncio.run(group_chat.on_chatbind_pick(cb, bot))

        raw = asyncio.run(db.get_setting(cities.per_city_key("delegate_chat_id", "spb")))
        assert raw == str(CHAT_ID)
        assert asyncio.run(db.get_setting("delegate_chat_id")) is None
        assert cb.message.edited


def test_chatbind_pick_rejects_unknown_city(tmp_path):
    with _cities_on(tmp_path):
        bot = FakeBot()
        cb = FakeCallback(f"chatbind:not-a-real-city:{CHAT_ID}", ADMIN_ID, _FakeChat(ADMIN_ID))

        asyncio.run(group_chat.on_chatbind_pick(cb, bot))

        assert cb.answers and cb.answers[0][1] is True  # show_alert
        assert asyncio.run(chat_tracking.bound_chats()) == []


def test_chatbind_pick_rejects_non_admin_user(tmp_path):
    with _cities_on(tmp_path):
        bot = FakeBot()
        cb = FakeCallback(f"chatbind:spb:{CHAT_ID}", STRANGER_ID, _FakeChat(STRANGER_ID))

        asyncio.run(group_chat.on_chatbind_pick(cb, bot))

        assert cb.answers and cb.answers[0][1] is True
        assert asyncio.run(chat_tracking.bound_chats()) == []


def test_chatbind_pick_by_a_different_admin_id_than_the_promoter_still_binds(tmp_path):
    """Фолбэк-веер уходит ВСЕМ `ADMIN_IDS` — привязывает первый ответивший, не обязательно
    исходный промоутер (`chat_id` едет В callback_data, а не берётся из чата коллбэка)."""
    with _cities_on(tmp_path):
        config.ADMIN_IDS = [ADMIN_ID, SECOND_ADMIN_ID]
        bot = FakeBot()
        cb = FakeCallback(f"chatbind:spb:{CHAT_ID}", SECOND_ADMIN_ID, _FakeChat(SECOND_ADMIN_ID))

        asyncio.run(group_chat.on_chatbind_pick(cb, bot))

        raw = asyncio.run(db.get_setting(cities.per_city_key("delegate_chat_id", "spb")))
        assert raw == str(CHAT_ID)


# ── D-7: без фолбэка на глобальный ключ при включённом модуле городов ────────────────────

def test_bound_chats_ignores_global_key_when_cities_module_on(tmp_path):
    with _cities_on(tmp_path):
        asyncio.run(db.set_setting("delegate_chat_id", str(CHAT_ID)))
        asyncio.run(db.set_setting("delegate_chat_title", "Общий чат (не должен утечь)"))

        assert asyncio.run(chat_tracking.bound_chats()) == []

        asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "СПб чат", "spb"))
        chats = asyncio.run(chat_tracking.bound_chats())
        assert len(chats) == 1
        assert chats[0]["city"] == "spb"
        assert chats[0]["chat_id"] == CHAT_ID


# ── chat_member: join/leave/kick ─────────────────────────────────────────────────────────

def test_chat_member_join_then_leave_writes_status_and_events(tmp_path):
    _ready(tmp_path)
    user = User(id=STRANGER_ID, is_bot=False, first_name="Ира")
    chat = Chat(id=CHAT_ID, type="supergroup")

    join_event = ChatMemberUpdated(
        chat=chat, from_user=user, date=datetime.now(),
        old_chat_member=_left_status(user), new_chat_member=_member_status(user),
    )
    asyncio.run(group_chat.on_chat_member_changed(join_event))
    row = asyncio.run(db.chat_member_row(CHAT_ID, STRANGER_ID))
    assert row["status"] == "member"
    assert row["joined_at"] is not None
    assert asyncio.run(_events_for(CHAT_ID, STRANGER_ID)) == ["join"]

    leave_event = ChatMemberUpdated(
        chat=chat, from_user=user, date=datetime.now(),
        old_chat_member=_member_status(user), new_chat_member=_left_status(user),
    )
    asyncio.run(group_chat.on_chat_member_changed(leave_event))
    row2 = asyncio.run(db.chat_member_row(CHAT_ID, STRANGER_ID))
    assert row2["status"] == "left"
    assert row2["left_at"] is not None
    assert asyncio.run(_events_for(CHAT_ID, STRANGER_ID)) == ["join", "leave"]


def test_chat_member_kick_writes_kick_event(tmp_path):
    _ready(tmp_path)
    user = User(id=STRANGER_ID, is_bot=False, first_name="Петя")
    chat = Chat(id=CHAT_ID, type="supergroup")
    asyncio.run(group_chat.on_chat_member_changed(ChatMemberUpdated(
        chat=chat, from_user=user, date=datetime.now(),
        old_chat_member=_left_status(user), new_chat_member=_member_status(user),
    )))
    asyncio.run(group_chat.on_chat_member_changed(ChatMemberUpdated(
        chat=chat, from_user=user, date=datetime.now(),
        old_chat_member=_member_status(user), new_chat_member=_kicked_status(user),
    )))
    assert asyncio.run(_events_for(CHAT_ID, STRANGER_ID)) == ["join", "kick"]


def test_chat_member_ignores_bots(tmp_path):
    _ready(tmp_path)
    bot_user = User(id=BOT_ID, is_bot=True, first_name="ДругойБот")
    chat = Chat(id=CHAT_ID, type="supergroup")
    asyncio.run(group_chat.on_chat_member_changed(ChatMemberUpdated(
        chat=chat, from_user=bot_user, date=datetime.now(),
        old_chat_member=_left_status(bot_user), new_chat_member=_member_status(bot_user),
    )))
    assert asyncio.run(db.chat_member_row(CHAT_ID, BOT_ID)) is None


# ── Фолбэк new_chat_members/left_chat_member ─────────────────────────────────────────────

def test_new_chat_members_fallback_writes_join(tmp_path):
    _ready(tmp_path)
    user = User(id=STRANGER_ID, is_bot=False, first_name="Оля")

    class _Msg:
        new_chat_members = [user]
        chat = Chat(id=CHAT_ID, type="supergroup")

    asyncio.run(group_chat.on_new_chat_members(_Msg()))
    row = asyncio.run(db.chat_member_row(CHAT_ID, STRANGER_ID))
    assert row["status"] == "member"
    assert row["source"] == "message"
    assert asyncio.run(_events_for(CHAT_ID, STRANGER_ID)) == ["join"]


def test_left_chat_member_fallback_writes_leave(tmp_path):
    _ready(tmp_path)
    user = User(id=STRANGER_ID, is_bot=False, first_name="Оля")
    asyncio.run(db.upsert_chat_member(CHAT_ID, STRANGER_ID, "member", source="chat_member"))

    class _Msg:
        left_chat_member = user
        chat = Chat(id=CHAT_ID, type="supergroup")

    asyncio.run(group_chat.on_left_chat_member(_Msg()))
    row = asyncio.run(db.chat_member_row(CHAT_ID, STRANGER_ID))
    assert row["status"] == "left"
    assert asyncio.run(_events_for(CHAT_ID, STRANGER_ID)) == ["leave"]


# ── catch-all: счётчики активности, НИКОГДА текст ────────────────────────────────────────

def test_catch_all_bumps_activity_when_tracking_on_and_chat_bound(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("chat_tracking_enabled", "on"))
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))

    asyncio.run(group_chat.on_group_message(FakeGroupMessage(STRANGER_ID, media_attr="photo")))

    async def _row():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT messages, replies, media FROM chat_activity WHERE chat_id = ? AND telegram_id = ?",
                (CHAT_ID, STRANGER_ID),
            ) as cursor:
                return await cursor.fetchone()

    assert asyncio.run(_row()) == (1, 0, 1)


def test_catch_all_noop_when_tracking_off(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))  # тумблер НЕ включён

    asyncio.run(group_chat.on_group_message(FakeGroupMessage(STRANGER_ID)))

    async def _count():
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM chat_activity") as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_count()) == 0


def test_catch_all_noop_for_unbound_chat(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("chat_tracking_enabled", "on"))  # включён, но чат не привязан

    asyncio.run(group_chat.on_group_message(FakeGroupMessage(STRANGER_ID)))

    async def _count():
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM chat_activity") as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_count()) == 0


def test_chat_tables_carry_no_text_columns(tmp_path):
    """D-9: три новые таблицы физически не могут хранить текст сообщения — колонок под
    контент в них нет вовсе."""
    _ready(tmp_path)
    forbidden = {"text", "message_text", "content", "caption", "body"}
    for table in ("chat_members", "chat_activity", "chat_events"):
        cols = asyncio.run(_table_columns(table))
        assert not (cols & forbidden), f"{table}: {cols & forbidden}"


# ── allowed_updates / роутер-порядок (D-4) ───────────────────────────────────────────────
#
# `handlers.registration.router` — модульный синглтон, уже подключён к СВОЕМУ Dispatcher'у
# внутри tests/test_refac_snapshot_260816.py (`_full_dispatcher()`, кэш на весь процесс) —
# повторный `include_router` в ЭТОМ файле уронил бы оба теста (`RuntimeError: Router is
# already attached`), если оба файла достанутся одному воркеру pytest-xdist. Поэтому личный
# роутер здесь — СВОЙ лёгкий `Router()` с единственным хендлером `Command("start")`,
# подставленный ВМЕСТО `registration.router` — тот же принцип (личный роутер после
# группового), без конфликта синглтонов.

_DISPATCHER_CACHE: dict = {}


def _group_dispatcher():
    """`group_chat.router` — тоже синглтон, `include_router` только ОДИН раз за жизнь
    процесса — общий кэш на оба теста ниже, которым он нужен."""
    if "dp" not in _DISPATCHER_CACHE:
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(group_chat.router)
        stub_personal_router = Router()
        calls: list = []

        @stub_personal_router.message(Command("start"))
        async def stub_cmd_start(message):
            calls.append(message)

        dp.include_router(stub_personal_router)
        _DISPATCHER_CACHE["dp"] = dp
        _DISPATCHER_CACHE["stub_calls"] = calls
    return _DISPATCHER_CACHE["dp"], _DISPATCHER_CACHE["stub_calls"]


def test_resolve_used_update_types_needs_group_chat_router():
    bare = Dispatcher(storage=MemoryStorage())
    bare.include_router(Router())
    bare_types = bare.resolve_used_update_types()
    assert "my_chat_member" not in bare_types
    assert "chat_member" not in bare_types

    dp, _calls = _group_dispatcher()
    used = dp.resolve_used_update_types()
    assert "my_chat_member" in used
    assert "chat_member" in used


def test_start_in_group_does_not_reach_the_personal_router_behind_it(tmp_path):
    _ready(tmp_path)
    dp, calls = _group_dispatcher()
    bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        chat = Chat(id=CHAT_ID, type="supergroup", title="Делегаты")
        user = User(id=STRANGER_ID, is_bot=False, first_name="Стажёр")
        msg = Message(message_id=1, date=datetime.now(), chat=chat, from_user=user, text="/start")
        update = Update(update_id=1, message=msg)
        asyncio.run(dp.feed_update(bot, update))
        assert len(calls) == 0  # съедено catch-all'ом group_chat.router, дальше не дошло
    finally:
        asyncio.run(bot.session.close())
