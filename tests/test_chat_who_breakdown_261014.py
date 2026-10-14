"""Дашборд «💬 Чат»: «Кто в чате» — ники участников, ник в ответах внешних форм, разбивка.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from aiogram.types import Chat, User

from config import config
from database import db
from handlers.chat import group_chat
from services import chat_tracking
from tests._dbtpl import fast_init_db

ADMIN_ID = 910801
CHAT_ID = -1001122334455


def _ready(tmp_path, name="who.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _usernames():
    async def go():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT telegram_id, username, first_name FROM chat_usernames"
            ) as cur:
                return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    return asyncio.run(go())


class _Bot:
    def __init__(self, users=None, fail=(), absent=()):
        self.calls = []
        self.users = users or {}
        self.fail = set(fail)
        self.absent = set(absent)

    async def get_chat_member(self, chat_id, telegram_id):
        self.calls.append(telegram_id)
        if telegram_id in self.fail:
            raise RuntimeError("boom")
        if telegram_id in self.absent:
            raise RuntimeError("Bad Request: PARTICIPANT_ID_INVALID")
        name, first = self.users.get(telegram_id, (None, None))
        return SimpleNamespace(
            status="member",
            user=SimpleNamespace(id=telegram_id, is_bot=False, username=name, first_name=first),
        )


# ── Задача 1: ники участников чата ───────────────────────────────────────────────────────

def test_refresh_chat_stores_username_and_first_name(tmp_path):
    _ready(tmp_path)

    async def go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status) VALUES (810001, 'A', 'approved')")
            await conn.commit()
        return await chat_tracking.refresh_chat(
            _Bot({810001: ("Nick_One", "Аня")}), CHAT_ID, None)

    asyncio.run(go())
    assert _usernames()[810001] == ("Nick_One", "Аня")


def test_chat_members_without_username_skips_known_and_left(tmp_path):
    _ready(tmp_path)

    async def go():
        for tid, st in ((1, "member"), (2, "member"), (3, "member"), (4, "left")):
            await db.upsert_chat_member(CHAT_ID, tid, st, source="refresh")
        await db.upsert_chat_username(2, None, "Имя")
        return await db.chat_members_without_username(CHAT_ID)

    assert asyncio.run(go()) == [1, 3]


def test_refresh_missing_usernames_asks_only_unknown(tmp_path):
    _ready(tmp_path)
    bot = _Bot({1: ("one", "Раз"), 3: ("three", "Три")}, fail={5}, absent={6})

    async def go():
        for tid in (1, 2, 3, 5, 6):
            await db.upsert_chat_member(CHAT_ID, tid, "member", source="refresh")
        await db.upsert_chat_username(2, "two", "Два")
        report = await chat_tracking.refresh_missing_usernames(bot, CHAT_ID)
        row6 = await db.chat_member_row(CHAT_ID, 6)
        return report, row6

    report, row6 = asyncio.run(go())
    assert sorted(bot.calls) == [1, 3, 5, 6]
    assert report["errors"] == 1 and report["left"] == 1 and report["named"] == 2
    assert row6["status"] == "left"
    names = _usernames()
    assert names[1][0] == "one" and names[3][0] == "three"


def test_refresh_missing_usernames_respects_cap_and_pause(tmp_path, monkeypatch):
    _ready(tmp_path)
    sleeps = []

    async def _sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(chat_tracking.asyncio, "sleep", _sleep)
    bot = _Bot()
    n = chat_tracking.REFRESH_BATCH + 3

    async def go():
        for tid in range(1, n + 1):
            await db.upsert_chat_member(CHAT_ID, tid, "member", source="refresh")
        full = await chat_tracking.refresh_missing_usernames(bot, CHAT_ID, max_calls=n)
        return full

    full = asyncio.run(go())
    assert full["checked"] == n and sleeps == [chat_tracking.REFRESH_PAUSE_SECONDS]
    # потолок
    _ready(tmp_path, "cap.db")
    bot2 = _Bot()

    async def go2():
        for tid in range(1, 6):
            await db.upsert_chat_member(CHAT_ID, tid, "member", source="refresh")
        return await chat_tracking.refresh_missing_usernames(bot2, CHAT_ID, max_calls=2)

    rep = asyncio.run(go2())
    assert rep["checked"] == 2 and rep["truncated"] is True


def test_refresh_all_chats_runs_username_catchup(tmp_path):
    _ready(tmp_path)
    bot = _Bot({77: ("seven", "Семь")})

    async def go():
        await db.set_setting("chat_tracking_enabled", "on")
        await chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Чат", None)
        await db.upsert_chat_member(CHAT_ID, 77, "member", source="chat_member")
        await chat_tracking.refresh_all_chats(bot)

    asyncio.run(go())
    assert 77 in bot.calls
    assert _usernames()[77][0] == "seven"


def test_join_events_store_username(tmp_path):
    _ready(tmp_path)
    from aiogram.types import ChatMemberUpdated, ChatMemberLeft, ChatMemberMember
    user = User(id=820001, is_bot=False, first_name="Ира", username="Ira_X")
    chat = Chat(id=CHAT_ID, type="supergroup")
    asyncio.run(group_chat.on_chat_member_changed(ChatMemberUpdated(
        chat=chat, from_user=user, date=datetime.now(),
        old_chat_member=ChatMemberLeft(user=user),
        new_chat_member=ChatMemberMember(user=user),
    )))
    assert _usernames()[820001] == ("Ira_X", "Ира")

    user2 = User(id=820002, is_bot=False, first_name="Оля", username="Olya_Y")

    class _Msg:
        new_chat_members = [user2]
        chat = Chat(id=CHAT_ID, type="supergroup")

    asyncio.run(group_chat.on_new_chat_members(_Msg()))
    assert _usernames()[820002] == ("Olya_Y", "Оля")


# ── Задача 2: username_needle в ответах внешней формы ────────────────────────────────────

from database import ext_forms_db as ef  # noqa: E402
from services.ext_forms import ext_forms_match as efm  # noqa: E402
from services.ext_forms.ext_forms_ingest import ingest_answer  # noqa: E402


def _form(**kw):
    fid = asyncio.run(ef.create_form(platform="yandex", external_id="f1", title="Ф", **kw))
    return asyncio.run(ef.get_form(fid))


def _items(nick):
    return [{"q": "nick", "label": "Ник в телеграме", "value": nick}]


def _needle(form_id, answer_id):
    row = asyncio.run(ef.get_answer(form_id, answer_id))
    return row["username_needle"], row["matched_telegram_id"]


def test_needle_filled_on_ingest(tmp_path):
    _ready(tmp_path)
    form = _form(key_username_q="nick")
    for aid, val, want in (("1", "@Nick_Two", "nick_two"), ("2", "https://t.me/Nick_Two", "nick_two"),
                           ("3", "-", ""), ("4", "Привет", ""), ("5", "", "")):
        asyncio.run(ingest_answer(form, answer_id=aid, answered_at=None, items=_items(val), raw=None))
        needle, matched = _needle(form["id"], aid)
        assert needle == want
        assert matched is None


def test_needle_null_without_username_question(tmp_path):
    _ready(tmp_path)
    form = _form()
    asyncio.run(ingest_answer(form, answer_id="1", answered_at=None, items=_items("@x_nick"), raw=None))
    assert _needle(form["id"], "1")[0] is None


def test_backfill_is_idempotent_and_rematch_calls_it(tmp_path):
    _ready(tmp_path)
    form = _form(key_username_q="nick")

    async def put(aid, val):
        await ef.insert_answer(form_id=form["id"], answer_id=aid, answered_at=None,
                               received_at="2026-10-14 10:00:00", payload=_items(val), raw=None,
                               matched_telegram_id=None, match_how=None)

    asyncio.run(put("1", "@Abc_Def"))
    asyncio.run(put("2", "нет"))
    assert _needle(form["id"], "1")[0] is None
    assert asyncio.run(efm.backfill_username_needles()) == 2
    assert _needle(form["id"], "1")[0] == "abc_def"
    assert _needle(form["id"], "2")[0] == ""
    assert asyncio.run(efm.backfill_username_needles()) == 0

    asyncio.run(put("3", "@Third_One"))
    asyncio.run(efm.rematch_unmatched())
    assert _needle(form["id"], "3")[0] == "third_one"


def test_set_form_keys_resets_needles(tmp_path):
    _ready(tmp_path)
    form = _form(key_username_q="nick")
    asyncio.run(ingest_answer(form, answer_id="1", answered_at=None, items=_items("@Zed_Zed"), raw=None))
    assert _needle(form["id"], "1")[0] == "zed_zed"
    asyncio.run(ef.set_form_keys(form["id"], "nick", None))
    assert _needle(form["id"], "1")[0] is None
    asyncio.run(efm.backfill_username_needles())
    assert _needle(form["id"], "1")[0] == "zed_zed"
