"""Имя бота из админки (`bot_name`, services/bot_profile.py): без BotFather, сразу после
сохранения; отказ Telegram (лимит частоты смены имени) — человеческим текстом."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SetMyName

from config import config
from database import db
import services.scheduler as sched
from services import bot_profile, miniapp_outbox
import settings_ops
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db


class _Bot:
    def __init__(self, current="Старое имя", fail=None):
        self.current = current
        self.fail = fail
        self.set_calls: list[str] = []
        self.sent: list[tuple[int, str]] = []

    async def get_my_name(self):
        return SimpleNamespace(name=self.current)

    async def set_my_name(self, name):
        if self.fail is not None:
            raise self.fail
        self.set_calls.append(name)
        self.current = name

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _ready(tmp_path, monkeypatch, bot=None):
    config.DB_PATH = str(tmp_path / "bot_name.db")
    fast_init_db()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


def _retry(seconds):
    return TelegramRetryAfter(method=SetMyName(name="x"), message="Too Many Requests", retry_after=seconds)


def test_key_is_in_registry_and_on_event_screen():
    from handlers.admin_settings import _EVENT_GROUP_KEYS

    entry = SETTINGS_SCHEMA["bot_name"]
    assert entry["group"] == "event" and entry["type"] == "text"
    assert "bot_name" in _EVENT_GROUP_KEYS
    assert "bot_description" in _EVENT_GROUP_KEYS  # описание «О боте» — на том же экране


def test_bot_process_applies_name_before_saving(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, _Bot())
    error = asyncio.run(settings_ops.cross_setting_error("bot_name", "Юлид’26 · регистрация"))
    assert error is None
    assert bot.set_calls == ["Юлид’26 · регистрация"]


def test_same_name_is_not_resent(tmp_path, monkeypatch):
    """Повтор того же имени (и старт бота) не тратит редкий лимит Telegram."""
    bot = _ready(tmp_path, monkeypatch, _Bot(current="Юлид"))
    assert asyncio.run(bot_profile.apply_bot_name(bot, "Юлид")) is None
    assert asyncio.run(bot_profile.apply_bot_name(bot, "  ")) is None
    assert bot.set_calls == []


def test_rate_limit_becomes_human_error(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, _Bot(fail=_retry(5400)))
    error = asyncio.run(settings_ops.cross_setting_error("bot_name", "Новое имя"))
    assert error and "1 ч 30 мин" in error and "прежним" in error
    assert "Flood" not in error and "Retry" not in error


def test_bad_request_and_network_errors_are_human(tmp_path, monkeypatch):
    bad = _Bot(fail=TelegramBadRequest(method=SetMyName(name="x"), message="Bad Request: name invalid"))
    _ready(tmp_path, monkeypatch, bad)
    error = asyncio.run(bot_profile.apply_bot_name(bad, "Имя"))
    assert error and "не принял" in error and "Bad Request" not in error

    down = _Bot(fail=RuntimeError("proxy down"))
    error = asyncio.run(bot_profile.apply_bot_name(down, "Имя"))
    assert error and "через минуту" in error and "proxy" not in error


def test_too_long_name_is_rejected_without_telegram(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, _Bot())
    error = asyncio.run(settings_ops.cross_setting_error("bot_name", "я" * 65))
    assert error and "64" in error
    assert bot.set_calls == []


def test_app_process_only_checks_length(tmp_path, monkeypatch):
    """В процессе приложения бота нет: проверка до записи не зовёт Telegram."""
    _ready(tmp_path, monkeypatch, None)
    assert asyncio.run(settings_ops.cross_setting_error("bot_name", "Имя")) is None
    check = asyncio.run(settings_ops.validate_batch_item(
        "bot_name", "я" * 70, visible_codes=[], selected_city=None, cities_on=False,
    ))
    assert check.error and "64" in check.error


def test_app_save_is_applied_by_bot_and_failure_reported_to_author(tmp_path, monkeypatch):
    ok = _ready(tmp_path, monkeypatch, _Bot())
    asyncio.run(db.set_setting("bot_name", "Из приложения"))
    asyncio.run(miniapp_outbox._handle_row(ok, "settings_changed", {"keys": ["bot_name"], "by": 42}))
    assert ok.set_calls == ["Из приложения"] and ok.sent == []

    limited = _Bot(fail=_retry(120))
    asyncio.run(miniapp_outbox._handle_row(limited, "settings_changed", {"keys": ["bot_name"], "by": 42}))
    assert len(limited.sent) == 1
    chat_id, text = limited.sent[0]
    assert chat_id == 42 and "2 мин" in text and "не применилось" in text


def test_saving_name_hook_does_not_call_telegram_twice(tmp_path, monkeypatch):
    """Хук записи `bot_name` не трогает: имя уже поставила проверка до записи."""
    from settings_audit import set_setting_by_admin

    bot = _ready(tmp_path, monkeypatch, _Bot())
    asyncio.run(set_setting_by_admin(1, "bot_name", "Имя"))
    assert bot.set_calls == []
