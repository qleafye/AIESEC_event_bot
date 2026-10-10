"""Живой учёт чата для рейтинга: журнал сообщений БЕЗ ТЕКСТА, текущие реакции, ники,
админы группы и срок хранения.

Владелец 20.09: «для отклика и реакций вживую нужна таблица message_id -> автор без текста».
Команда = staff + ADMIN_IDS + админы группы. pytest-asyncio нет — `asyncio.run()`; БД —
`fast_init_db`.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from tests._dbtpl import fast_init_db

ADMIN = 900927501
CHAT = -1009275001
A, B, C = 900927511, 900927512, 900927513


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "capture.db")
    config.ADMIN_IDS = [ADMIN]
    fast_init_db()


async def _rows(sql, *params):
    async with db._connect() as conn:
        async with conn.execute(sql, params) as cur:
            return await cur.fetchall()


def _log(mid, author, ts="2026-09-27 10:00:00", **kw):
    params = dict(kind="text", text_len=10, reply_to_message_id=None, reply_to_author_id=None,
                  is_channel_post=False)
    params.update(kw)
    return db.log_chat_message(CHAT, mid, author, ts, **params)


# ── Задача 1: схема и хелперы ─────────────────────────────────────────────────────────────

def test_tables_exist_and_init_is_idempotent(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A))
    _run(db.init_db())
    names = {r[0] for r in _run(_rows("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"chat_messages", "chat_reactions", "chat_usernames", "chat_admins"} <= names
    assert _run(_rows("SELECT COUNT(*) FROM chat_messages"))[0][0] == 1
    cols = {r[1] for r in _run(_rows("PRAGMA table_info(chat_messages)"))}
    assert not [c for c in cols if "text" in c and c != "text_len"]


def test_log_chat_message_ignores_duplicates(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, text_len=10))
    _run(_log(1, A, text_len=99))
    rows = _run(_rows("SELECT text_len, kind, source FROM chat_messages"))
    assert rows == [(10, "text", "live")]


def test_update_len_only_for_existing_row(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, text_len=10))
    _run(db.update_chat_message_len(CHAT, 1, 42))
    _run(db.update_chat_message_len(CHAT, 2, 42))
    assert _run(_rows("SELECT message_id, text_len FROM chat_messages")) == [(1, 42)]


def test_set_chat_reactions_mirrors_current_state(tmp_path):
    _ready(tmp_path)
    ts = "2026-09-27 10:05:00"
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍", "🔥"], ts))
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍"], ts))
    assert _run(_rows("SELECT reaction FROM chat_reactions")) == [("👍",)]
    _run(db.set_chat_reactions(CHAT, 1, B, [], ts))
    assert _run(_rows("SELECT COUNT(*) FROM chat_reactions"))[0][0] == 0


def test_upsert_chat_username(tmp_path):
    _ready(tmp_path)
    _run(db.upsert_chat_username(A, "@alice"))
    _run(db.upsert_chat_username(A, None))
    _run(db.upsert_chat_username(A, ""))
    assert _run(_rows("SELECT username FROM chat_usernames WHERE telegram_id = ?", A)) == [("alice",)]
    _run(db.upsert_chat_username(A, "alice2"))
    assert _run(_rows("SELECT username FROM chat_usernames WHERE telegram_id = ?", A)) == [("alice2",)]


def test_replace_chat_admins(tmp_path):
    _ready(tmp_path)
    _run(db.replace_chat_admins(CHAT, [A, B]))
    _run(db.replace_chat_admins(CHAT, [B]))
    assert _run(_rows("SELECT telegram_id FROM chat_admins WHERE chat_id = ?", CHAT)) == [(B,)]


def test_prune_chat_history(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A, ts="2026-01-01 10:00:00"))
    _run(_log(2, A, ts="2026-09-27 10:00:00"))
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍"], "2026-09-27 10:00:00"))  # сообщения уже нет
    _run(db.set_chat_reactions(CHAT, 2, B, ["👍"], "2026-01-02 10:00:00"))  # старая реакция
    _run(db.set_chat_reactions(CHAT, 2, C, ["🔥"], "2026-09-27 11:00:00"))
    counts = _run(db.prune_chat_history("2026-06-01 00:00:00"))
    assert counts["messages"] == 1
    assert _run(_rows("SELECT message_id FROM chat_messages")) == [(2,)]
    assert _run(_rows("SELECT telegram_id FROM chat_reactions")) == [(C,)]


def test_purge_user_clears_all_chat_traces(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A))
    _run(_log(2, B, reply_to_message_id=1, reply_to_author_id=A))
    _run(db.set_chat_reactions(CHAT, 2, A, ["👍"], "2026-09-27 10:00:00"))
    _run(db.upsert_chat_username(A, "alice"))
    _run(db.replace_chat_admins(CHAT, [A, C]))
    _run(db.purge_user(A))
    assert _run(_rows("SELECT telegram_id, reply_to_author_id FROM chat_messages")) == [(B, None)]
    assert _run(_rows("SELECT COUNT(*) FROM chat_reactions"))[0][0] == 0
    assert _run(_rows("SELECT COUNT(*) FROM chat_usernames"))[0][0] == 0
    assert _run(_rows("SELECT telegram_id FROM chat_admins")) == [(C,)]


def test_purge_chat_data_clears_per_chat_tables(tmp_path):
    _ready(tmp_path)
    _run(_log(1, A))
    _run(db.set_chat_reactions(CHAT, 1, B, ["👍"], "2026-09-27 10:00:00"))
    _run(db.replace_chat_admins(CHAT, [A]))
    _run(db.upsert_chat_username(A, "alice"))
    _run(db.purge_chat_data(CHAT))
    for table in ("chat_messages", "chat_reactions", "chat_admins"):
        assert _run(_rows(f"SELECT COUNT(*) FROM {table}"))[0][0] == 0, table
    assert _run(_rows("SELECT COUNT(*) FROM chat_usernames"))[0][0] == 1  # ники — не по чату


# ── Задача 2: групповые хендлеры — журнал, правки, реакции ─────────────────────────────────

from datetime import datetime, timezone  # noqa: E402

from aiogram.types import (  # noqa: E402
    Chat, Dice, ForumTopicCreated, Message, MessageOriginUser, MessageReactionUpdated,
    PhotoSize, ReactionTypeCustomEmoji, ReactionTypeEmoji, ReactionTypePaid, Sticker, User,
)

from handlers.chat import group_chat  # noqa: E402
from services import chat_tracking  # noqa: E402

UTC_DATE = datetime(2026, 9, 27, 7, 30, 0, tzinfo=timezone.utc)  # 10:30 по Москве
GROUP = Chat(id=CHAT, type="supergroup", title="Делегаты")


def _user(uid, **kw):
    return User(id=uid, is_bot=False, first_name="Имя", **kw)


def _msg(mid, uid=A, **kw):
    kw.setdefault("chat", GROUP)
    return Message(message_id=mid, date=UTC_DATE, from_user=_user(uid, username=kw.pop("username", None)), **kw)


def _live(tmp_path, *, tracking=True, bound=True):
    _ready(tmp_path)
    if tracking:
        _run(db.set_setting("chat_tracking_enabled", "on"))
    if bound:
        _run(chat_tracking.bind_chat(ADMIN, CHAT, "Делегаты", None))


def _logged():
    return _run(_rows(
        "SELECT message_id, telegram_id, ts, kind, text_len, reply_to_message_id, "
        "reply_to_author_id, is_channel_post FROM chat_messages ORDER BY message_id"
    ))


def test_text_message_is_logged_without_text(tmp_path):
    _live(tmp_path)
    _run(group_chat.on_group_message(_msg(1, text="ы" * 120, username="alice")))
    assert _logged() == [(1, A, "2026-09-27 10:30:00", "text", 120, None, None, 0)]
    assert _run(_rows("SELECT username FROM chat_usernames WHERE telegram_id = ?", A)) == [("alice",)]


def test_kinds_and_forwarded_length(tmp_path):
    _live(tmp_path)
    photo = [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    sticker = Sticker(file_id="s", file_unique_id="su", type="regular", width=1, height=1,
                      is_animated=False, is_video=False)
    _run(group_chat.on_group_message(_msg(1, photo=photo, caption="к" * 30)))
    _run(group_chat.on_group_message(_msg(2, sticker=sticker)))
    _run(group_chat.on_group_message(_msg(3, dice=Dice(emoji="🎲", value=3))))
    origin = MessageOriginUser(date=UTC_DATE, sender_user=_user(C))
    _run(group_chat.on_group_message(_msg(4, text="чужой текст", forward_origin=origin)))
    rows = {r[0]: (r[3], r[4]) for r in _logged()}
    assert rows == {1: ("media", 30), 2: ("sticker", 0), 3: ("other", 0), 4: ("text", 0)}


def test_real_reply_in_normal_group(tmp_path):
    _live(tmp_path)
    target = _msg(1, uid=B, text="вопрос")
    _run(group_chat.on_group_message(_msg(2, text="ответ", reply_to_message=target)))
    assert _logged()[0][5:7] == (1, B)


def test_topic_root_is_not_a_reply_but_real_reply_in_topic_is(tmp_path):
    _live(tmp_path)
    root = Message(message_id=100, date=UTC_DATE, chat=GROUP, from_user=_user(ADMIN),
                   forum_topic_created=ForumTopicCreated(name="Флуд", icon_color=0))
    _run(group_chat.on_group_message(_msg(
        101, text="привет", reply_to_message=root, is_topic_message=True, message_thread_id=100,
    )))
    target = _msg(102, uid=B, text="вопрос", is_topic_message=True, message_thread_id=100)
    _run(group_chat.on_group_message(_msg(
        103, text="ответ", reply_to_message=target, is_topic_message=True, message_thread_id=100,
    )))
    rows = {r[0]: (r[5], r[6]) for r in _logged()}
    assert rows[101] == (None, None)
    assert rows[103] == (102, B)


def test_automatic_channel_forward_is_a_channel_post(tmp_path):
    _live(tmp_path)
    channel = Chat(id=-1009999, type="channel", title="Канал")
    post = Message(message_id=200, date=UTC_DATE, chat=GROUP, is_automatic_forward=True,
                   sender_chat=channel, from_user=User(id=777000, is_bot=False, first_name="Telegram"),
                   text="п" * 300)
    _run(group_chat.on_group_message(post))
    _run(group_chat.on_group_message(_msg(201, text="комментарий", reply_to_message=post)))
    rows = {r[0]: r for r in _logged()}
    assert rows[200][1] == -1009999 and rows[200][4] == 300 and rows[200][7] == 1
    assert rows[201][5:7] == (200, None)
    activity = _run(_rows("SELECT telegram_id FROM chat_activity"))
    assert (777000,) not in activity and (-1009999,) not in activity


def test_reply_to_bot_or_anonymous_admin_has_no_author(tmp_path):
    _live(tmp_path)
    bot_msg = Message(message_id=300, date=UTC_DATE, chat=GROUP,
                      from_user=User(id=5, is_bot=True, first_name="Бот"), text="я бот")
    anon = Message(message_id=301, date=UTC_DATE, chat=GROUP, sender_chat=GROUP,
                   from_user=User(id=1087968824, is_bot=True, first_name="Group"), text="аноним")
    _run(group_chat.on_group_message(_msg(302, text="а", reply_to_message=bot_msg)))
    _run(group_chat.on_group_message(_msg(303, text="б", reply_to_message=anon)))
    rows = {r[0]: (r[5], r[6]) for r in _logged()}
    assert rows == {302: (300, None), 303: (301, None)}


def test_nothing_logged_when_tracking_off_or_unbound(tmp_path):
    _live(tmp_path, tracking=False)
    _run(group_chat.on_group_message(_msg(1, text="раз")))
    assert _logged() == []
    _run(db.set_setting("chat_tracking_enabled", "on"))
    other = Chat(id=-100123, type="supergroup")
    _run(group_chat.on_group_message(_msg(2, text="два", chat=other)))
    assert _logged() == []


def test_edited_message_updates_length(tmp_path):
    _live(tmp_path)
    _run(group_chat.on_group_message(_msg(1, text="коротко")))
    _run(group_chat.on_group_edited_message(_msg(1, text="д" * 77)))
    assert _logged()[0][4] == 77


def _reaction(new, user=None, actor_chat=None):
    return MessageReactionUpdated(
        chat=GROUP, message_id=1, date=UTC_DATE, old_reaction=[], new_reaction=new,
        user=user, actor_chat=actor_chat,
    )


def test_reactions_are_mirrored(tmp_path):
    _live(tmp_path)
    _run(group_chat.on_group_reaction(_reaction([ReactionTypeEmoji(emoji="👍")], user=_user(B))))
    _run(group_chat.on_group_reaction(_reaction(
        [ReactionTypeCustomEmoji(custom_emoji_id="555"), ReactionTypePaid()], user=_user(C),
    )))
    _run(group_chat.on_group_reaction(_reaction([ReactionTypeEmoji(emoji="🔥")], actor_chat=GROUP)))
    rows = sorted(_run(_rows("SELECT telegram_id, reaction, ts FROM chat_reactions")))
    assert rows == [
        (B, "👍", "2026-09-27 10:30:00"),
        (C, "custom:555", "2026-09-27 10:30:00"),
        (C, "paid", "2026-09-27 10:30:00"),
    ]
    _run(group_chat.on_group_reaction(_reaction([], user=_user(B))))
    assert [r[0] for r in _run(_rows("SELECT telegram_id FROM chat_reactions"))] == [C, C]


def test_update_types_include_reactions_and_edits():
    from tests.test_chat_binding_260914 import _group_dispatcher
    dp, _calls = _group_dispatcher()
    used = dp.resolve_used_update_types()
    assert "message_reaction" in used
    assert "edited_message" in used


def test_group_chat_touches_text_only_for_length():
    import inspect
    src = inspect.getsource(group_chat)
    lines = [ln for ln in src.splitlines() if "message.text" in ln or "message.caption" in ln]
    code_lines = [ln for ln in lines if not ln.strip().startswith(("#", "`", '"'))]
    assert code_lines and all("len(" in ln for ln in code_lines), code_lines


# ── Задача 3: сверка админов группы и чистка старой истории ─────────────────────────────────

from types import SimpleNamespace  # noqa: E402

from services import scheduler as sched  # noqa: E402

BOT_ID = 777927501


def _adm(uid, *, is_bot=False, status="administrator", can_delete=True):
    return SimpleNamespace(
        status=status, can_delete_messages=can_delete,
        user=SimpleNamespace(id=uid, is_bot=is_bot),
    )


class _AdminsBot:
    id = BOT_ID

    def __init__(self, admins, *, fail=False):
        self.admins = admins
        self.fail = fail
        self.calls = 0

    async def get_chat_administrators(self, chat_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network")
        return self.admins

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


def test_refresh_chat_admins_replaces_and_sets_bot_state(tmp_path):
    _ready(tmp_path)
    bot = _AdminsBot([_adm(A, status="creator"), _adm(B), _adm(BOT_ID, is_bot=True, can_delete=False),
                      _adm(5, is_bot=True)])
    _run(chat_tracking.refresh_chat_admins(bot, CHAT))
    ids = sorted(r[0] for r in _run(_rows("SELECT telegram_id FROM chat_admins WHERE chat_id = ?", CHAT)))
    assert ids == sorted([A, B])
    state = _run(db.get_chat_bot_state(CHAT))
    assert (state["bot_status"], state["can_delete"]) == ("administrator", 0)


def test_refresh_chat_admins_bot_absent_is_member(tmp_path):
    _ready(tmp_path)
    _run(chat_tracking.refresh_chat_admins(_AdminsBot([_adm(A)]), CHAT))
    state = _run(db.get_chat_bot_state(CHAT))
    assert (state["bot_status"], state["can_delete"]) == ("member", 0)


def test_refresh_chat_admins_failure_keeps_previous(tmp_path):
    _ready(tmp_path)
    _run(db.replace_chat_admins(CHAT, [A]))
    _run(chat_tracking.refresh_chat_admins(_AdminsBot([], fail=True), CHAT))
    assert _run(_rows("SELECT telegram_id FROM chat_admins")) == [(A,)]


def test_refresh_all_chats_and_bind_reconcile_sync_admins(tmp_path, monkeypatch):
    _live(tmp_path)
    bot = _AdminsBot([_adm(B)])
    _run(chat_tracking.refresh_all_chats(bot))
    assert bot.calls == 1
    assert _run(_rows("SELECT telegram_id FROM chat_admins")) == [(B,)]

    bot2 = _AdminsBot([_adm(C)])
    bot2.send_message = lambda *a, **k: _noop()
    monkeypatch.setattr(sched, "_bot", bot2)
    _run(chat_tracking.bind_reconcile_job(CHAT, None, ADMIN))
    assert bot2.calls == 1
    assert _run(_rows("SELECT telegram_id FROM chat_admins")) == [(C,)]


async def _noop():
    return None


def test_prune_job_uses_retention_even_with_tracking_off(tmp_path):
    _ready(tmp_path)  # учёт выключен: срок хранения — про приватность, а не про учёт
    from services.timeutil import msk_now
    from datetime import timedelta as _td
    old = (msk_now() - _td(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (msk_now() - _td(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    _run(_log(1, A, ts=old))
    _run(_log(2, A, ts=fresh))
    _run(sched.chat_history_prune_job())
    assert _run(_rows("SELECT message_id FROM chat_messages")) == [(2,)]
    _run(db.set_setting("chat_rating_retention_days", "5"))
    _run(sched.chat_history_prune_job())
    assert _run(_rows("SELECT COUNT(*) FROM chat_messages"))[0][0] == 0


def test_prune_job_is_registered_daily_with_boot_catchup(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "prune_sched.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)

    async def go():
        fast_init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            job = s.get_job("chat_history_prune")
            assert job is not None
            assert job.trigger.interval.total_seconds() == 24 * 3600
            assert job.func is sched.chat_history_prune_job
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())
