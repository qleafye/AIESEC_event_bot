"""Команды кнопки «Меню» из настроек (`services/bot/bot_commands.py`): /start всем, /admin только
организаторам (личный список), английская версия через language_code, не задано/пусто — не
трогать; 429 — повтор, правки склеиваются, бывшим организаторам /admin снимается."""
from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from config import config
from database import db
import services.scheduler as sched
from services.bot import bot_commands
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

_ALL = {
    "bot_command_start_text": "Главное меню", "bot_command_admin_text": "Панель организатора",
    "bot_command_start_text_en": "Main menu", "bot_command_admin_text_en": "Organizer panel",
}


class _Bot:
    def __init__(self, unknown=(), retry_once=False):
        self.calls: list[tuple[list[tuple[str, str]], object, str | None]] = []
        self.deleted: list[tuple[int, str | None]] = []
        self.unknown = set(unknown)
        self.retry_once = retry_once

    async def set_my_commands(self, commands, scope=None, language_code=None):
        if self.retry_once:
            self.retry_once = False
            raise TelegramRetryAfter(method=SetMyCommands(commands=[]), message="Too Many Requests",
                                     retry_after=0)
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id in self.unknown:
            raise RuntimeError("chat not found")
        self.calls.append(([(c.command, c.description) for c in commands], scope, language_code))

    async def delete_my_commands(self, scope=None, language_code=None):
        self.deleted.append((scope.chat_id, language_code))


def _ready(tmp_path, monkeypatch, admin_ids=(1,), values=_ALL):
    config.DB_PATH = str(tmp_path / "bot_commands.db")
    fast_init_db()
    monkeypatch.setattr(config, "ADMIN_IDS", list(admin_ids))
    monkeypatch.setattr(bot_commands, "CALL_PAUSE_SECONDS", 0)
    monkeypatch.setattr(bot_commands, "DEBOUNCE_SECONDS", 0)
    for key, value in values.items():
        asyncio.run(db.set_setting(key, value))


def _by_scope(bot):
    out = {}
    for cmds, scope, lang in bot.calls:
        who = "all" if isinstance(scope, BotCommandScopeDefault) else scope.chat_id
        out[(who, lang)] = cmds
    return out


def test_keys_on_event_screen_without_defaults():
    from handlers.settings.admin_settings import _EVENT_GROUP_KEYS

    for key in bot_commands.COMMAND_KEYS:
        assert SETTINGS_SCHEMA[key]["group"] == "event"
        assert SETTINGS_SCHEMA[key]["default"] is None  # иначе первый старт перетёр бы BotFather
        assert key in _EVENT_GROUP_KEYS


def test_clean_db_touches_nothing(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, values={})
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert bot.calls == [] and bot.deleted == []


def test_delegates_see_start_only_organizers_see_admin(tmp_path, monkeypatch):
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
    _ready(tmp_path, monkeypatch, values={**_ALL, "bot_command_start_text_en": "",
                                          "bot_command_admin_text": "  "})
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert set(_by_scope(bot)) == {("all", None)}  # en не тронут, личных списков на русском нет


def test_former_organizer_loses_admin_command(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=(1,))
    asyncio.run(db.add_staff(5, "moderator", 1))
    asyncio.run(bot_commands.sync_bot_commands(_Bot()))
    asyncio.run(db.remove_staff(5, "moderator"))
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert sorted(bot.deleted, key=str) == sorted([(5, None), (5, "en")], key=str)
    bot = _Bot()
    asyncio.run(bot_commands.sync_bot_commands(bot))
    assert bot.deleted == []  # снят один раз


def test_unknown_chat_and_429_do_not_stop_others(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=(1, 2))
    bot = _Bot(unknown={1}, retry_once=True)
    asyncio.run(bot_commands.sync_bot_commands(bot))
    got = _by_scope(bot)
    assert ("all", None) in got  # первый запрос получил 429 и прошёл повтором
    assert (2, None) in got and (2, "en") in got


def test_saves_are_coalesced_into_one_sync(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, admin_ids=(), values={})
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(bot_commands, "DEBOUNCE_SECONDS", 1.5)  # окно шире четырёх записей

    async def scenario():
        for key, value in _ALL.items():
            await set_setting_by_admin(1, key, value)
        await bot_commands._pending
        before = len(bot.calls)
        await set_setting_by_admin(1, "event_name", "Юлид")
        return before

    before = asyncio.run(scenario())
    assert _by_scope(bot)[("all", None)] == [("start", "Главное меню")]
    assert before == len(bot.calls) == 2  # один синк на четыре правки: ru + en
