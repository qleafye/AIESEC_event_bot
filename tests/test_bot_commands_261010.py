"""Команды кнопки «Меню» из настроек (`services/bot_commands.py`): /start всем, /admin только
организаторам (личный список), английская версия через language_code, пустое — не трогать."""
from __future__ import annotations

import asyncio

from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from config import config
from database import db
import services.scheduler as sched
from services import bot_commands
from settings_audit import set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db


class _Bot:
    def __init__(self, unknown=()):
        self.calls: list[tuple[list[tuple[str, str]], object, str | None]] = []
        self.unknown = set(unknown)

    async def set_my_commands(self, commands, scope=None, language_code=None):
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id in self.unknown:
            raise RuntimeError("chat not found")
        self.calls.append(([(c.command, c.description) for c in commands], scope, language_code))


def _ready(tmp_path, monkeypatch, admin_ids=(1,)):
    config.DB_PATH = str(tmp_path / "bot_commands.db")
    fast_init_db()
    monkeypatch.setattr(config, "ADMIN_IDS", list(admin_ids))


def _by_scope(bot):
    out = {}
    for cmds, scope, lang in bot.calls:
        who = "all" if isinstance(scope, BotCommandScopeDefault) else scope.chat_id
        out[(who, lang)] = cmds
    return out


def test_keys_on_event_screen():
    from handlers.admin_settings import _EVENT_GROUP_KEYS

    for key in bot_commands.COMMAND_KEYS:
        assert SETTINGS_SCHEMA[key]["group"] == "event"
        assert key in _EVENT_GROUP_KEYS


def test_defaults_delegates_see_start_only_organizers_see_admin(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=(1,))
    asyncio.run(db.add_staff(5, "moderator", 1))
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    got = _by_scope(bot)
    assert got[("all", None)] == [("start", "Главное меню")]
    assert got[("all", "en")] == [("start", "Main menu")]
    for uid in (1, 5):
        assert got[(uid, None)] == [("start", "Главное меню"), ("admin", "Панель организатора")]
        assert got[(uid, "en")] == [("start", "Main menu"), ("admin", "Organizer panel")]
    assert len(got) == 6


def test_expired_role_gets_no_admin_command(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=())
    asyncio.run(db.add_staff(7, "moderator", 1))
    asyncio.run(db.set_staff_expiry(7, "moderator", "2000-01-01"))
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert all(isinstance(scope, BotCommandScopeDefault) for _, scope, _ in bot.calls)


def test_empty_values_leave_lists_untouched(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    asyncio.run(db.set_setting("bot_command_start_text_en", ""))
    asyncio.run(db.set_setting("bot_command_admin_text", "  "))
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    got = _by_scope(bot)
    assert set(got) == {("all", None)}  # en не тронут, личных списков на русском нет

    asyncio.run(db.set_setting("bot_command_start_text", ""))
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert bot.calls == []


def test_unknown_chat_does_not_stop_others(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=(1, 2))
    bot = _Bot(unknown={1})
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert (2, None) in _by_scope(bot)


def test_saving_description_applies_right_away(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=())
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot", bot)
    asyncio.run(set_setting_by_admin(1, "bot_command_start_text", "Начать"))
    assert _by_scope(bot)[("all", None)] == [("start", "Начать")]

    bot.calls.clear()
    asyncio.run(set_setting_by_admin(1, "event_name", "Юлид"))
    assert bot.calls == []
