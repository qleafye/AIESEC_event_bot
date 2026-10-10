"""Описание бота из настроек (`services.bot_profile`): запрос РилТолка 04.10 — менеджер сам
задаёт текст пустого чата до /start и строку «О боте», без BotFather."""
from __future__ import annotations

import asyncio

from config import config
from database import db
import services.scheduler as sched
from services import bot_profile
from services.settings.audit import set_setting_by_admin
from tests._dbtpl import fast_init_db


class _Bot:
    def __init__(self):
        self.calls = []

    async def set_my_description(self, description):
        self.calls.append(("description", description))

    async def set_my_short_description(self, short_description):
        self.calls.append(("short", short_description))


def _ready(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "bot_profile.db")
    fast_init_db()
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


def test_saving_description_applies_it_to_telegram_right_away(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch)
    asyncio.run(set_setting_by_admin(1, "bot_description", "Новости — t.me/realtalkforum26"))
    assert ("description", "Новости — t.me/realtalkforum26") in bot.calls
    assert ("short", "") in bot.calls


def test_unrelated_setting_does_not_touch_profile(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch)
    asyncio.run(set_setting_by_admin(1, "start_text", "Привет"))
    assert bot.calls == []


def test_overlong_texts_are_cut_to_telegram_limits(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch)
    asyncio.run(db.set_setting("bot_description", "д" * 600))
    asyncio.run(db.set_setting("bot_short_description", "к" * 200))
    asyncio.run(bot_profile.sync_bot_profile(bot))
    assert ("description", "д" * 512) in bot.calls
    assert ("short", "к" * 120) in bot.calls


def test_telegram_failure_does_not_break_saving(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch)

    async def boom(**kw):
        raise RuntimeError("telegram down")

    bot.set_my_description = boom
    asyncio.run(set_setting_by_admin(1, "bot_description", "текст"))
    assert asyncio.run(db.get_setting("bot_description")) == "текст"
