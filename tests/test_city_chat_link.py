"""Закрытая ссылка чата города: таблицы, запрос к Telegram, отказ отдать старый URL.

pytest-asyncio нет — корутины через asyncio.run. Схема — копия fast_init_db.
Клиент приглашений — фейк без aiogram, как описано в плане выпуска ссылки.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from config import config
from database import city_chat_db, db
from handlers.access.admin_caps import role_caps_key
from services import chat_tracking, city_chat_link
from tests._dbtpl import fast_init_db

SUPER = 11001
SPB = 11002
MSK = 11003
UNBOUND = 11004
CHAT = -10011011


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="city-chat.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


class Invite:
    def __init__(self, link, *, revoked=False, join=True, expire=None, limit=None):
        self.invite_link = link
        self.is_revoked = revoked
        self.creates_join_request = join
        self.expire_date = expire
        self.member_limit = limit


class Client:
    def __init__(self):
        self.edits = []
        self.creates = []
        self.revokes = []
        self.edit_result = None
        self.edit_exc = None
        self.create_exc = None
        self.revoke_exc = None
        self._n = 0

    async def edit_invite(self, chat_id, invite_link, name, expire_date, member_limit):
        self.edits.append((chat_id, invite_link, name, expire_date, member_limit))
        if self.edit_exc:
            raise self.edit_exc
        return self.edit_result

    async def create_invite(self, chat_id, name, expire_date, member_limit, creates_join_request):
        self.creates.append((chat_id, name, expire_date, member_limit, creates_join_request))
        if self.create_exc:
            raise self.create_exc
        self._n += 1
        return Invite(
            f"https://t.me/+new{self._n}",
            join=creates_join_request,
            expire=expire_date,
            limit=member_limit,
        )

    async def revoke_invite(self, chat_id, invite_link):
        self.revokes.append((chat_id, invite_link))
        if self.revoke_exc:
            raise self.revoke_exc
        return Invite(invite_link, revoked=True)


def test_schema_idempotent(tmp_path):
    _ready(tmp_path)

    async def go():
        async with db._connect() as conn:
            await city_chat_db.ensure_schema(conn)
            await city_chat_db.ensure_schema(conn)

    _run(go())


def test_link_roundtrip_is_per_city(tmp_path):
    _ready(tmp_path)

    async def go():
        await city_chat_db.save_link(
            "spb", chat_id=CHAT, invite_link="https://t.me/+spb",
            creates_join_request=1, expire_date=111, member_limit=None, lifetime_days=14,
        )
        row = await city_chat_db.get_link("spb")
        assert row["chat_id"] == CHAT
        assert row["invite_link"] == "https://t.me/+spb"
        assert row["creates_join_request"] == 1
        assert row["expire_date"] == 111
        assert row["member_limit"] is None
        assert row["lifetime_days"] == 14
        assert await city_chat_db.get_link("msk") is None
        await city_chat_db.save_link(None, chat_id=-1, invite_link="https://t.me/+all")
        shared = await city_chat_db.get_link(None)
        assert shared["city"] == ""
        assert shared["invite_link"] == "https://t.me/+all"
        assert await city_chat_db.get_link("spb") is not None

    _run(go())


def test_allow_hides_only_that_chat(tmp_path):
    _ready(tmp_path)

    async def go():
        await city_chat_db.allow(CHAT, 42, added_by=SUPER)
        assert await city_chat_db.is_allowed(CHAT, 42) is True
        assert await city_chat_db.is_allowed(CHAT, 43) is False
        assert await city_chat_db.is_allowed(-100, 42) is False

    _run(go())


def test_summary_notice_roundtrip(tmp_path):
    _ready(tmp_path)

    async def go():
        assert await city_chat_db.summary_notice(CHAT, SUPER) is None
        await city_chat_db.save_summary_notice(CHAT, SUPER, 777)
        row = await city_chat_db.summary_notice(CHAT, SUPER)
        assert row["message_id"] == 777
        assert await city_chat_db.summary_notice(CHAT, SPB) is None
        await city_chat_db.clear_summary_notice(CHAT, SUPER)
        assert await city_chat_db.summary_notice(CHAT, SUPER) is None

    _run(go())


def test_module_does_not_import_aiogram():
    env = os.environ.copy()
    env.setdefault("BOT_TOKEN", "123456:dummy-test-token")
    env.setdefault("ADMIN_IDS", "[1]")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import services.city_chat_link, sys; assert 'aiogram' not in sys.modules"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_export_and_member_tables_not_used():
    text = open("services/city_chat_link.py", encoding="utf-8").read()
    assert "exportChatInviteLink" not in text
    assert "export_chat_invite_link" not in text
    assert "chat_events" not in text
    assert "chat_members" not in text
    db_text = open("database/city_chat_db.py", encoding="utf-8").read()
    assert "aiogram" not in db_text
    assert "chat_members" not in db_text
    init_text = open("database/db.py", encoding="utf-8").read()
    assert "city_chat_db.ensure_schema" in init_text
    assert "CREATE TABLE city_chat" not in init_text


async def _bind(city=None):
    await chat_tracking.bind_chat(SUPER, CHAT, "Делегаты", city)


def test_default_issue_is_join_request_without_limit(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind()
        client = Client()
        result = await city_chat_link.issue(client, None)
        assert result.ok is True
        assert result.url == "https://t.me/+new1"
        assert client.edits == []
        chat_id, name, expire, limit, join = client.creates[0]
        assert chat_id == CHAT
        assert expire is None and limit is None and join is True
        assert len(name) <= 32
        assert "creates_join_request" not in (result.error or "")

    _run(go())


def test_issue_probes_before_url_even_if_stored_expire_is_past(tmp_path, monkeypatch):
    _ready(tmp_path)
    now = 1_700_000_000
    monkeypatch.setattr(city_chat_link.time, "time", lambda: now)

    async def go():
        await _bind()
        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old",
            creates_join_request=1, expire_date=now - 500, lifetime_days=14,
        )
        client = Client()
        client.edit_result = Invite("https://t.me/+live", join=True, expire=now + 999)
        result = await city_chat_link.issue(client, None)
        assert result.ok is True
        assert result.url == "https://t.me/+live"
        assert client.creates == []
        assert client.edits[0][3] == now - 500

    _run(go())


def test_revoked_mints_new_and_dead_revoke_is_ignored(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind()
        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old", creates_join_request=1,
        )
        client = Client()
        client.edit_exc = Exception("INVITE_HASH_EXPIRED")
        client.revoke_exc = Exception("INVITE_HASH_EXPIRED")
        result = await city_chat_link.issue(client, None)
        assert result.ok is True
        assert result.url == "https://t.me/+new1"
        assert "https://t.me/+old" != result.url
        assert client.revokes == [(CHAT, "https://t.me/+old")]
        assert client.creates[0][4] is True

    _run(go())


def test_expired_in_response_mints_from_now(tmp_path, monkeypatch):
    _ready(tmp_path)
    now = 1_700_000_000
    monkeypatch.setattr(city_chat_link.time, "time", lambda: now)

    async def go():
        await _bind()
        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old",
            creates_join_request=0, expire_date=now - 10, member_limit=50, lifetime_days=14,
        )
        client = Client()
        client.edit_result = Invite("https://t.me/+old", join=False, expire=now - 10, limit=50)
        result = await city_chat_link.issue(client, None)
        assert result.url == "https://t.me/+new1"
        expected = now + 14 * 86400
        assert client.creates[0][2] == expected
        assert client.creates[0][3] == 50
        assert client.creates[0][4] is False
        saved = await city_chat_db.get_link(None)
        assert saved["expire_date"] == expected
        assert saved["expire_date"] != now - 10

    _run(go())


def test_limit_reached_is_dead(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind()
        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old",
            creates_join_request=0, member_limit=50,
        )
        client = Client()
        client.edit_exc = Exception("INVITE_HASH_EXPIRED")
        result = await city_chat_link.issue(client, None)
        assert result.url == "https://t.me/+new1"
        assert client.creates[0][3] == 50

    _run(go())


def test_transport_failure_hides_url(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind()
        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old", creates_join_request=1,
        )
        for text in ("chat not found", "429", "Forbidden"):
            client = Client()
            client.edit_exc = Exception(text)
            result = await city_chat_link.issue(client, None)
            assert result.ok is False
            assert result.url is None
            assert client.creates == []
            assert "https://t.me/+old" not in (result.error or "")
            assert result.error == city_chat_link._CHECK_FAILED

    _run(go())


def test_no_bound_chat_does_not_call_telegram(tmp_path):
    _ready(tmp_path)

    async def go():
        client = Client()
        result = await city_chat_link.issue(client, "spb")
        assert result.ok is False and result.url is None
        assert client.edits == [] and client.creates == [] and client.revokes == []
        assert "не подключён" in result.error
        assert "delegate_chat_id" not in result.error
        assert "creates_join_request" not in result.error
        assert "spb" not in result.error

    _run(go())


def test_policy_immediate_with_limit_and_join_drops_limit(tmp_path, monkeypatch):
    _ready(tmp_path)
    now = 1_700_000_000
    monkeypatch.setattr(city_chat_link.time, "time", lambda: now)

    async def go():
        await _bind()
        client = Client()
        first = await city_chat_link.issue(client, None)
        opened = await city_chat_link.set_policy(
            client, None, join_requests=False, member_limit=50, lifetime_days=14,
        )
        assert opened.url == "https://t.me/+new2"
        assert client.revokes[-1] == (CHAT, first.url)
        chat_id, name, expire, limit, join = client.creates[-1]
        assert join is False and limit == 50 and expire == now + 14 * 86400
        asked = await city_chat_link.set_policy(
            client, None, join_requests=True, member_limit=50, lifetime_days=None,
        )
        assert asked.ok is True
        assert client.creates[-1][3] is None
        assert client.creates[-1][4] is True
        bad_days = await city_chat_link.set_policy(
            client, None, join_requests=False, member_limit=50, lifetime_days=0,
        )
        assert bad_days.ok is False and "например 14" in bad_days.error
        bad_limit = await city_chat_link.set_policy(
            client, None, join_requests=False, member_limit=0, lifetime_days=14,
        )
        assert bad_limit.ok is False and "например 200" in bad_limit.error

    _run(go())


def test_describe_words_and_does_not_mint_without_a_link(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind()
        client = Client()
        text = await city_chat_link.describe(client, None)
        assert text == city_chat_link._NO_LINK
        assert client.creates == [] and client.edits == []
        assert "пускают по заявке" not in text

        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old",
            creates_join_request=1, lifetime_days=None,
        )
        client.edit_result = Invite("https://t.me/+old", join=True, expire=None)
        joined = await city_chat_link.describe(client, None)
        assert "пускают по заявке" in joined
        assert "без срока" in joined and "без лимита" in joined
        for banned in ("creates_join_request", "delegate_chat_id", "member_limit"):
            assert banned not in joined

        await city_chat_db.save_link(
            None, chat_id=CHAT, invite_link="https://t.me/+old",
            creates_join_request=0, member_limit=50, lifetime_days=14,
        )
        client.edit_result = Invite("https://t.me/+old", join=False, expire=None, limit=50)
        opened = await city_chat_link.describe(client, None)
        assert "пускает сразу" in opened
        assert "на 14 дней" in opened and "не больше 50 человек" in opened
        assert "выгоняет" in opened

        client.edit_exc = Exception("429")
        failed = await city_chat_link.describe(client, None)
        assert "Не смог проверить" in failed
        assert "пускают по заявке" not in failed
        assert "пускает сразу" not in failed

    _run(go())


def test_recipients_skip_other_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [SUPER])

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "settings")
        await db.add_staff(SPB, "reg_manager", SUPER)
        await db.set_staff_city(SPB, "spb")
        await db.add_staff(MSK, "reg_manager", SUPER)
        await db.set_staff_city(MSK, "msk")
        await db.add_staff(UNBOUND, "reg_manager", SUPER)

        got = await city_chat_link.recipients_for_city("spb")
        assert SUPER in got and SPB in got and UNBOUND in got
        assert MSK not in got

        await db.set_setting("event_city_enabled", "off")
        everyone = await city_chat_link.recipients_for_city("spb")
        assert MSK in everyone and SPB in everyone

    _run(go())


def test_recipients_do_not_fall_back_to_every_holder(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "settings")
        await db.add_staff(MSK, "reg_manager", 1)
        await db.set_staff_city(MSK, "msk")
        assert await city_chat_link.recipients_for_city("spb") == []

    _run(go())


def test_link_name_is_cut_to_telegram_limit(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _bind("spb")
        client = Client()
        await city_chat_link.issue(client, "spb")
        assert len(client.creates[0][1]) <= 32
        assert client.creates[0][1].startswith("Делегаты")

    _run(go())
