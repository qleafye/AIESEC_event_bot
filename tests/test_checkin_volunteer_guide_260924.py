"""Форум-ночь B3 (идея №22): шпаргалка волонтёра чек-ина — личное сообщение человеку, который
только что получил роль/право с capability `checkin` (`handlers/admin_roles.py::roles_assign`).

Покрывает:
- роль несёт `checkin` (role_caps_<role> настроен менеджером) -> новое назначение шлёт текст
  из реестра `checkin_volunteer_guide_text`;
- роль НЕ несёт `checkin` (дефолтные caps reg_manager/game_manager/stats_manager) -> сообщение
  не уходит;
- повторное назначение той же роли тому же человеку («Уже был в этой роли») -> сообщение НЕ
  дублируется (created=False);
- сбой `bot.send_message` (делегат заблокировал бота) -> fail-soft, роль всё равно назначена.

Стиль — тот же приём, что tests/test_role_caps_buttons_260813.py (`set_setting(role_caps_key
(...))`) + tests/test_roles_phase8.py (FakeCallback/FakeBot, asyncio.run, без pytest-asyncio)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from database.db import set_setting
from handlers import admin_roles
from handlers.admin_caps import role_caps_key
from tests._dbtpl import fast_init_db

ADMIN_ID = 910301
VOLUNTEER_ID = 910302

ROLE = "game_manager"  # дефолтные caps = ["moderate_game"], без checkin -- удобно переключать


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_checkin_volunteer_guide_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    async def edit_text(self, *a, **k):
        return None


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeBot:
    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self.fail:
            raise RuntimeError("delegate blocked the bot")
        self.sent.append((chat_id, text))


def test_role_with_checkin_cap_sends_guide_on_new_assignment(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(set_setting(role_caps_key(ROLE), "checkin"))
    bot = _FakeBot()

    cb = _FakeCallback(f"roles_addrole:{VOLUNTEER_ID}:{ROLE}")
    asyncio.run(admin_roles.roles_assign(cb, bot))

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == VOLUNTEER_ID
    assert "Сканер" in text
    assert "🟢" in text and "🟡" in text and "🔴" in text


def test_role_without_checkin_cap_sends_nothing(tmp_path):
    _db_ready(tmp_path)  # game_manager дефолт -- только moderate_game
    bot = _FakeBot()

    cb = _FakeCallback(f"roles_addrole:{VOLUNTEER_ID}:{ROLE}")
    asyncio.run(admin_roles.roles_assign(cb, bot))

    assert bot.sent == []


def test_repeated_assignment_does_not_resend_guide(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(set_setting(role_caps_key(ROLE), "checkin"))
    bot = _FakeBot()

    asyncio.run(admin_roles.roles_assign(_FakeCallback(f"roles_addrole:{VOLUNTEER_ID}:{ROLE}"), bot))
    asyncio.run(admin_roles.roles_assign(_FakeCallback(f"roles_addrole:{VOLUNTEER_ID}:{ROLE}"), bot))

    assert len(bot.sent) == 1


def test_blocked_bot_does_not_prevent_role_assignment(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(set_setting(role_caps_key(ROLE), "checkin"))
    bot = _FakeBot(fail=True)

    cb = _FakeCallback(f"roles_addrole:{VOLUNTEER_ID}:{ROLE}")
    asyncio.run(admin_roles.roles_assign(cb, bot))  # не должно бросить исключение

    assert asyncio.run(db.get_staff_roles(VOLUNTEER_ID)) == [ROLE]
    assert bot.sent == []


def test_guide_text_is_editable_via_registry(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(set_setting(role_caps_key(ROLE), "checkin"))
    asyncio.run(set_setting("checkin_volunteer_guide_text", "Кастомный текст шпаргалки"))
    bot = _FakeBot()

    cb = _FakeCallback(f"roles_addrole:{VOLUNTEER_ID}:{ROLE}")
    asyncio.run(admin_roles.roles_assign(cb, bot))

    assert bot.sent == [(VOLUNTEER_ID, "Кастомный текст шпаргалки")]
