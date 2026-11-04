"""Чат города: кто свой, кого показать в сводке, можно ли принять заявку.

pytest-asyncio нет — корутины через asyncio.run. Схема — копия fast_init_db.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import domain.cities as cities
from config import config
from database import city_chat_db, db
from services import chat_tracking, city_chat_gate
from tests._dbtpl import fast_init_db

SUPER = 12001
CHAT = -10012011
OTHER_CHAT = -10012012
STRAY = -10012099

NO_BOT = 22001
REJECTED = 22002
OTHER = 22003
PENDING = 22004
OURS = 22005


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="city-gate.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [SUPER]
    fast_init_db()


@contextmanager
def _registry():
    saved = list(cities.CITIES)
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": ""},
            {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб"},
        ])
        yield
    finally:
        cities.set_cities_for_test(saved)


async def _user(tid, status, event_city, full_name="Имя", username=None):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, event_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, full_name, username, status, event_city),
        )
        await conn.commit()


async def _member(chat_id, tid):
    await db.upsert_chat_member(chat_id, tid, "member", source="gate")


async def _bind_msk():
    await db.set_setting("event_city_enabled", "on")
    await chat_tracking.bind_chat(SUPER, CHAT, "Делегаты", "msk")


def test_gate_does_not_import_aiogram():
    env = os.environ.copy()
    env.setdefault("BOT_TOKEN", "123456:dummy-test-token")
    env.setdefault("ADMIN_IDS", "[1]")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import services.city_chat_gate, sys; assert 'aiogram' not in sys.modules"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_reasons_order(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _member(CHAT, NO_BOT)
            await _user(REJECTED, "rejected", "spb", full_name="Отказ")
            await _member(CHAT, REJECTED)
            await _user(OTHER, "approved", "spb", full_name="Чужой")
            await _member(CHAT, OTHER)
            await _user(PENDING, "pending", "msk", full_name="Ждёт")
            await _member(CHAT, PENDING)
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(CHAT, OURS)
            rows = await city_chat_gate.outsider_rows(CHAT)
        reasons = [row["reason"] for row in rows]
        assert reasons == [
            city_chat_gate.REASON_NO_BOT,
            city_chat_gate.REASON_REJECTED,
            city_chat_gate.REASON_OTHER_CITY,
            city_chat_gate.REASON_NOT_APPROVED,
        ]
        assert [row["telegram_id"] for row in rows] == [NO_BOT, REJECTED, OTHER, PENDING]
        assert OURS not in [row["telegram_id"] for row in rows]

    _run(go())


def test_module_off_approved_is_ours(tmp_path):
    _ready(tmp_path)

    async def go():
        await chat_tracking.bind_chat(SUPER, CHAT, "Делегаты", None)
        bound = await chat_tracking.bound_chats()
        assert bound == [{"city": None, "chat_id": CHAT, "title": "Делегаты"}]
        await _user(OURS, "approved", "spb", full_name="Свой")
        await _member(CHAT, OURS)
        await _user(PENDING, "pending", "tyumen", full_name="Ждёт")
        await _member(CHAT, PENDING)
        rows = await city_chat_gate.outsider_rows(CHAT)
        assert [row["telegram_id"] for row in rows] == [PENDING]
        assert rows[0]["reason"] == city_chat_gate.REASON_NOT_APPROVED
        assert city_chat_gate.REASON_OTHER_CITY not in [row["reason"] for row in rows]

    _run(go())


def test_allow_hides_next_summary(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _user(REJECTED, "rejected", "spb", full_name="Отказ")
            await _member(CHAT, REJECTED)
            assert any(row["telegram_id"] == REJECTED for row in await city_chat_gate.outsider_rows(CHAT))
            await city_chat_db.allow(CHAT, REJECTED, added_by=SUPER)
            assert await city_chat_db.is_allowed(CHAT, REJECTED) is True
            assert all(row["telegram_id"] != REJECTED for row in await city_chat_gate.outsider_rows(CHAT))

    _run(go())


def test_may_approve_only_ours(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk")
            await _user(OTHER, "approved", "spb")
            await _user(PENDING, "pending", "msk")
            await _user(REJECTED, "rejected", "msk")
            assert await city_chat_gate.may_approve(CHAT, OURS) is True
            assert await city_chat_gate.may_approve(CHAT, OTHER) is False
            assert await city_chat_gate.may_approve(CHAT, PENDING) is False
            assert await city_chat_gate.may_approve(CHAT, REJECTED) is False
            assert await city_chat_gate.may_approve(CHAT, NO_BOT) is False
            assert await city_chat_gate.may_approve(STRAY, OURS) is False

    _run(go())


def test_names_hide_empty_and_skip_contacts(tmp_path):
    _ready(tmp_path)
    source = open("services/city_chat_gate.py", encoding="utf-8").read()
    assert "phone" not in source
    assert "email" not in source

    async def go():
        with _registry():
            await _bind_msk()
            await _user(NO_BOT, "pending", "msk", full_name="   ", username="@Nick")
            await db.upsert_chat_username(NO_BOT, "other", "  ")
            await _member(CHAT, NO_BOT)
            rows = await city_chat_gate.outsider_rows(CHAT)
        assert rows[0]["name"] == "без имени и ника"
        assert rows[0]["username"] == "Nick"

    _run(go())


def test_unbound_chat_is_not_global(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(OTHER_CHAT, OURS)
            await _member(STRAY, PENDING)
            assert await city_chat_gate.outsider_rows(STRAY) == []
            assert await city_chat_gate.outsider_rows(OTHER_CHAT) == []
            assert await city_chat_gate.may_approve(STRAY, OURS) is False

    _run(go())


class _Screen:
    def __init__(self):
        self.text = ""
        self.markup = None

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.markup = kwargs.get("reply_markup")


class _Cb:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _Screen()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))


class _Bot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.bodies = {}
        self.approved = []
        self.declined = []
        self.bans = []
        self.unbans = []
        self.approve_exc = None
        self.ban_exc = None
        self._n = 500

    async def send_message(self, chat_id, text, **kwargs):
        self._n += 1
        self.sent.append((chat_id, text, self._n))
        self.bodies[(chat_id, self._n)] = text
        return SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, text, *, chat_id, message_id, **kwargs):
        self.edits.append((chat_id, message_id, text))
        key = (chat_id, message_id)
        if self.bodies.get(key) == text:
            raise Exception("Bad Request: message is not modified")
        self.bodies[key] = text

    async def approve_chat_join_request(self, chat_id, user_id):
        self.approved.append((chat_id, user_id))
        if self.approve_exc:
            raise self.approve_exc

    async def decline_chat_join_request(self, *args, **kwargs):
        self.declined.append((args, kwargs))

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.bans.append((chat_id, user_id))
        if self.ban_exc:
            raise self.ban_exc

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.unbans.append((chat_id, user_id))


def _event(chat_id, user_id):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
    )


def test_join_request_approves_only_ours(tmp_path, caplog):
    _ready(tmp_path)
    caplog.set_level(logging.WARNING)

    async def go():
        from handlers.chat.city_chat_join import on_chat_join_request

        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk")
            await _user(OTHER, "approved", "spb")
            bot = _Bot()
            await on_chat_join_request(_event(CHAT, OURS), bot)
            assert bot.approved == [(CHAT, OURS)]
            assert bot.declined == []
            await on_chat_join_request(_event(CHAT, OTHER), bot)
            assert bot.approved == [(CHAT, OURS)]
            bot.approve_exc = Exception("https://t.me/+secret")
            await on_chat_join_request(_event(CHAT, OURS), bot)
            assert bot.declined == []

    _run(go())
    assert "Exception" in caplog.text
    assert "https://" not in caplog.text
    assert "t.me" not in caplog.text


def test_join_observer_registered():
    from aiogram import Dispatcher, Router
    from aiogram.fsm.storage.memory import MemoryStorage
    from handlers.chat import group_chat

    bare = Dispatcher(storage=MemoryStorage())
    bare.include_router(Router())
    assert "chat_join_request" not in bare.resolve_used_update_types()
    # group_chat.router — синглтон: второй include_router роняет тесты привязки.
    assert "chat_join_request" in group_chat.router.resolve_used_update_types()


def test_approved_morning_does_not_notify(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.city_chat_summary_send import note_join

        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(CHAT, OURS)
            await _user(PENDING, "pending", "msk", full_name="Ждёт")
            await _member(CHAT, PENDING)
            bot = _Bot()
            await note_join(bot, CHAT, PENDING)
            assert len(bot.sent) == 1
            sent_n, edit_n = len(bot.sent), len(bot.edits)
            await note_join(bot, CHAT, OURS)
            assert len(bot.sent) == sent_n and len(bot.edits) == edit_n
            await city_chat_db.allow(CHAT, PENDING, added_by=SUPER)
            await note_join(bot, CHAT, PENDING)
            assert len(bot.sent) == sent_n and len(bot.edits) == edit_n

    _run(go())


def test_name_is_escaped(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.city_chat_summary_send import note_join

        with _registry():
            await _bind_msk()
            await _user(PENDING, "pending", "msk", full_name="<b>A & B</b>")
            await _member(CHAT, PENDING)
            bot = _Bot()
            await note_join(bot, CHAT, PENDING)
        text = bot.sent[0][1]
        assert "<b>" not in text
        assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in text

    _run(go())


def test_burst_one_message(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.city_chat_summary_send import note_join

        with _registry():
            await _bind_msk()
            await _user(PENDING, "pending", "msk", full_name="Аня")
            await _user(REJECTED, "rejected", "msk", full_name="Боря")
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(CHAT, PENDING)
            await _member(CHAT, REJECTED)
            await _member(CHAT, OURS)
            bot = _Bot()
            await note_join(bot, CHAT, PENDING)
            await note_join(bot, CHAT, OURS)
            await note_join(bot, CHAT, REJECTED)
            await note_join(bot, CHAT, PENDING)
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == SUPER
        assert len(bot.edits) == 2
        assert bot.edits[0][1] == bot.sent[0][2]
        body = bot.edits[0][2]
        assert body.count("Аня") == 1
        assert body.count("Боря") == 1

    _run(go())


def test_burst_window_reopens(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.city_chat_summary_send import note_join

        with _registry():
            await _bind_msk()
            await _user(PENDING, "pending", "msk", full_name="Аня")
            await _user(REJECTED, "rejected", "msk", full_name="Боря")
            await _member(CHAT, PENDING)
            await _member(CHAT, REJECTED)
            bot = _Bot()
            await note_join(bot, CHAT, PENDING)
            async with db._connect() as conn:
                await conn.execute(
                    "UPDATE city_chat_summary_notice SET opened_at = ? WHERE chat_id = ?",
                    ("2000-01-01 00:00:00", CHAT),
                )
                await conn.commit()
            await note_join(bot, CHAT, REJECTED)
        assert len(bot.sent) == 2
        assert bot.edits == []

    _run(go())


def test_leave_hides_and_remove_needs_confirm(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.admin_city_chat_summary import on_summary_button

        with _registry():
            await _bind_msk()
            await _user(PENDING, "pending", "msk", full_name="Аня")
            await _user(REJECTED, "rejected", "msk", full_name="Боря")
            await _member(CHAT, PENDING)
            await _member(CHAT, REJECTED)
            bot = _Bot()
            await on_summary_button(_Cb(f"cchat:keep:{CHAT}:{PENDING}", SUPER), bot)
            assert await city_chat_db.is_allowed(CHAT, PENDING) is True
            left = await city_chat_gate.outsider_rows(CHAT)
            assert all(row["telegram_id"] != PENDING for row in left)

            ask = _Cb(f"cchat:ask:{CHAT}:{REJECTED}", SUPER)
            await on_summary_button(ask, bot)
            assert "Убрать" in ask.message.text and "Боря" in ask.message.text
            assert "Делегаты" in ask.message.text
            assert bot.bans == []

            skip = _Cb(f"cchat:no:{CHAT}:{REJECTED}", SUPER)
            await on_summary_button(skip, bot)
            assert bot.bans == []

            bot.ban_exc = Exception("not enough rights")
            denied = _Cb(f"cchat:yes:{CHAT}:{REJECTED}", SUPER)
            await on_summary_button(denied, bot)
            assert bot.unbans == []
            assert "нет права исключать" in denied.message.text
            assert await city_chat_db.is_allowed(CHAT, REJECTED) is False

            bot.ban_exc = None
            bot.bans.clear()
            yes = _Cb(f"cchat:yes:{CHAT}:{REJECTED}", SUPER)
            await on_summary_button(yes, bot)
            assert bot.bans == [(CHAT, REJECTED)]
            assert bot.unbans == [(CHAT, REJECTED)]
            assert "Убрал" in yes.message.text

    _run(go())


def test_stale_yes_does_not_kick_approved(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.admin_city_chat_summary import on_summary_button

        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(CHAT, OURS)
            bot = _Bot()
            cb = _Cb(f"cchat:yes:{CHAT}:{OURS}", SUPER)
            await on_summary_button(cb, bot)
        assert bot.bans == []
        assert any("в сводке уже нет" in (text or "") for text, _alert in cb.answers)

    _run(go())


def test_foreign_manager_blocked(tmp_path):
    _ready(tmp_path)

    async def go():
        from handlers.chat.admin_city_chat_summary import on_summary_button

        with _registry():
            await _bind_msk()
            await _user(REJECTED, "rejected", "msk", full_name="Боря")
            await _member(CHAT, REJECTED)
            bot = _Bot()
            keep = _Cb(f"cchat:keep:{CHAT}:{REJECTED}", 99999)
            await on_summary_button(keep, bot)
            yes = _Cb(f"cchat:yes:{CHAT}:{REJECTED}", 99999)
            await on_summary_button(yes, bot)
            assert await city_chat_db.is_allowed(CHAT, REJECTED) is False
            assert bot.bans == []
            assert "другого города" in (keep.answers[0][0] or "")
            assert "другого города" in (yes.answers[0][0] or "")

    _run(go())


def test_handlers_do_not_read_message_text():
    names = (
        "handlers/chat/city_chat_join.py",
        "handlers/chat/city_chat_summary_send.py",
        "handlers/chat/admin_city_chat_summary.py",
        "services/city_chat_gate.py",
    )
    for name in names:
        text = open(name, encoding="utf-8").read()
        assert "message.text" not in text
        assert "decline_chat_join_request" not in text
        assert "notify_by_capability" not in text
        assert "export_chat_invite_link" not in text
    for name in names[:2] + names[3:]:
        assert "ban_chat_member" not in open(name, encoding="utf-8").read()
    admin = open("handlers/chat/admin_city_chat_summary.py", encoding="utf-8").read()
    assert "ban_chat_member" in admin
    group = open("handlers/chat/group_chat.py", encoding="utf-8").read()
    assert "note_join" in group
    assert "ban_chat_member" not in group
    assert "bot: Bot | None = None" in group
