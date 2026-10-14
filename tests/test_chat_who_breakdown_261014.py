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


# ── Задача 3: дашборд «Кто в чате» ───────────────────────────────────────────────────────

import sqlite3  # noqa: E402

from dashboard import db as dash_db  # noqa: E402
from dashboard.queries import Scope, chat_form_delegations, chat_who_breakdown  # noqa: E402
from tests.test_dashboard_chat_260914 import _cfg, _client, _login, STATS_MANAGER_ID  # noqa: E402

MSK_CHAT = {"city": "msk", "chat_id": CHAT_ID, "title": "Чат", "label": "Москва"}


async def _seed_who():
    async with db._connect() as conn:
        for code, label, order in (("msk", "Москва", 0), ("spb", "Санкт-Петербург", 1)):
            await conn.execute(
                "INSERT INTO cities (code, label, enabled, sort_order, created_at) "
                "VALUES (?, ?, 1, ?, '2026-01-01 00:00:00')", (code, label, order))
        users = [(1, "approved", "msk"), (2, "approved", "spb"), (3, "rejected", None),
                 (4, "pending", None), (5, "waitlist", None), (13, "approved", "msk")]
        for tid, st, city in users:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status, event_city) VALUES (?, 'X', ?, ?)",
                (tid, st, city))
        # 6 делегация, 7 начал анкету, 8 админ, 9 никто, 13 ушёл
        for tid in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            await conn.execute(
                "INSERT INTO chat_members (chat_id, telegram_id, status, source, updated_at) "
                "VALUES (?, ?, 'member', 't', '2026-01-01 00:00:00')", (CHAT_ID, tid))
        await conn.execute(
            "INSERT INTO chat_members (chat_id, telegram_id, status, source, updated_at) "
            "VALUES (?, 13, 'left', 't', '2026-01-01 00:00:00')", (CHAT_ID,))
        await conn.execute("INSERT INTO chat_usernames (telegram_id, username, updated_at) "
                           "VALUES (6, 'NICK_DEL', '2026-01-01')")
        await conn.execute("INSERT INTO reg_started (telegram_id, started_at) VALUES (7, '2026-01-01')")
        await conn.execute("INSERT INTO chat_admins (chat_id, telegram_id, synced_at) "
                           "VALUES (?, 8, '2026-01-01')", (CHAT_ID,))
        await conn.commit()


def _seed_form_answer(needle_text="@Nick_Del"):
    form = asyncio.run(ef.create_form(platform="yandex", external_id="f1", title="Ф",
                                      key_username_q="nick"))
    asyncio.run(ingest_answer({"id": form, "key_username_q": "nick"}, answer_id="1",
                              answered_at=None, items=_items(needle_text), raw=None))
    return form


def _breakdown(path, scope=None, chat=MSK_CHAT):
    with dash_db.read_conn(path) as conn:
        return chat_who_breakdown(conn, scope or Scope(), chat)


def test_who_breakdown_each_category_once_and_sum_equals_present(tmp_path):
    config.DB_PATH = path = str(tmp_path / "who3.db")
    fast_init_db()
    asyncio.run(_seed_who())
    _seed_form_answer()
    res = _breakdown(path)
    counts = {r["key"]: r["count"] for r in res["rows"]}
    assert counts == {"approved_here": 1, "approved_other": 1, "rejected": 1, "pending": 1,
                      "other_status": 1, "delegation": 1, "started": 1, "chat_admin": 1,
                      "unidentified": 1}
    assert res["present"] == 9 == sum(counts.values())
    assert res["no_application"] == 4


def test_who_breakdown_priority(tmp_path):
    config.DB_PATH = path = str(tmp_path / "who4.db")
    fast_init_db()

    async def seed():
        async with db._connect() as conn:
            for tid in (10, 11, 12):
                await conn.execute(
                    "INSERT INTO chat_members (chat_id, telegram_id, status, source, updated_at) "
                    "VALUES (?, ?, 'member', 't', '2026-01-01')", (CHAT_ID, tid))
                await conn.execute("INSERT INTO chat_admins (chat_id, telegram_id, synced_at) "
                                   "VALUES (?, ?, '2026-01-01')", (CHAT_ID, tid))
            for tid in (10, 11):
                await conn.execute("INSERT INTO reg_started (telegram_id, started_at) "
                                   "VALUES (?, '2026-01-01')", (tid,))
            await conn.execute("INSERT INTO chat_usernames (telegram_id, username, updated_at) "
                               "VALUES (10, '@Mixed_Case', '2026-01-01')")
            await conn.commit()

    asyncio.run(seed())
    _seed_form_answer("@mixed_case")
    counts = {r["key"]: r["count"] for r in _breakdown(path)["rows"]}
    assert counts["delegation"] == 1 and counts["started"] == 1 and counts["chat_admin"] == 1


def test_who_breakdown_spb_chat_and_scope(tmp_path):
    config.DB_PATH = path = str(tmp_path / "who5.db")
    fast_init_db()
    asyncio.run(_seed_who())
    spb_chat = {"city": "spb", "chat_id": CHAT_ID, "title": "СПб", "label": "СПб"}
    counts = {r["key"]: r["count"] for r in _breakdown(path, chat=spb_chat)["rows"]}
    # user 1 (msk) и user 2 (spb): в чате spb «этот город» только второй
    assert counts["approved_here"] == 1 and counts["approved_other"] == 1
    other = _breakdown(path, scope=Scope(city="spb"))
    assert other["present"] == 0 and all(r["count"] == 0 for r in other["rows"])


def test_who_breakdown_without_needle_column(tmp_path):
    config.DB_PATH = path = str(tmp_path / "who6.db")
    fast_init_db()
    asyncio.run(_seed_who())
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX IF EXISTS idx_ext_answers_needle")
    raw.execute("ALTER TABLE external_form_answers DROP COLUMN username_needle")
    raw.commit()
    raw.close()
    res = _breakdown(path)
    assert {r["key"]: r["count"] for r in res["rows"]}["delegation"] == 0
    assert res["present"] == 9
    with dash_db.read_conn(path) as conn:
        assert chat_form_delegations(conn, Scope(), MSK_CHAT) is None


def test_form_delegations_counts_users_and_non_users(tmp_path):
    config.DB_PATH = path = str(tmp_path / "who7.db")
    fast_init_db()
    asyncio.run(_seed_who())
    with dash_db.read_conn(path) as conn:
        assert chat_form_delegations(conn, Scope(), MSK_CHAT) is None  # форм нет
    form = asyncio.run(ef.create_form(platform="yandex", external_id="f1", title="Ф",
                                      key_username_q="nick"))
    fdict = {"id": form, "key_username_q": "nick"}

    async def put():
        await db.upsert_chat_username(9, "Zed", None)
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET username = 'UserOne' WHERE telegram_id = 1")
            await conn.commit()
        for aid, val in (("a", "@nick_del"), ("b", "@userone"), ("c", "@zed"), ("d", "@gone_far"),
                         ("e", "без ника")):
            await ingest_answer(fdict, answer_id=aid, answered_at=None, items=_items(val), raw=None)
        async with db._connect() as conn:
            await conn.execute("UPDATE external_form_answers SET matched_telegram_id = 2 "
                               "WHERE answer_id = 'e'")
            await conn.commit()

    asyncio.run(put())
    with dash_db.read_conn(path) as conn:
        res = chat_form_delegations(conn, Scope(), MSK_CHAT)
    # a -> id6, b -> id1 (users), c -> id9, e -> matched 2 (в чате); d вне чата
    assert res == {"answers": 5, "in_chat": 4}


def test_chat_route_shows_who_block(tmp_path):
    config.DB_PATH = path = str(tmp_path / "who8.db")
    fast_init_db()
    asyncio.run(_seed_who())

    async def staff():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at, city) "
                "VALUES (?, 'reg_manager', 1, '2026-01-01', NULL)", (STATS_MANAGER_ID,))
            for k, v in (("role_caps_reg_manager", "moderate_reg;stats"),
                         ("delegate_chat_id", str(CHAT_ID)), ("delegate_chat_title", "Чат")):
                await conn.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?)", (k, v))
            await conn.commit()

    asyncio.run(staff())
    _seed_form_answer()
    client = _client(_cfg(path))
    _login(client, STATS_MANAGER_ID)
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert "Кто в чате" in resp.text
    assert "Ответов во внешней форме: 1" in resp.text
    assert "В чате без заявки" in resp.text
