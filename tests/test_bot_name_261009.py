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
import domain.settings.ops as settings_ops
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA
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


def test_precheck_never_calls_telegram(tmp_path, monkeypatch):
    """До записи — только длина и пустота: запись ещё может не состояться (подтверждение,
    отмена), а имя в Telegram уже сменилось бы."""
    bot = _ready(tmp_path, monkeypatch, _Bot())
    assert asyncio.run(settings_ops.cross_setting_error("bot_name", "Юлид’26 · регистрация")) is None
    assert "64" in asyncio.run(settings_ops.cross_setting_error("bot_name", "я" * 65))
    assert "пустым" in asyncio.run(bot_profile.precheck_bot_name("   "))
    check = asyncio.run(settings_ops.validate_batch_item(
        "bot_name", "я" * 70, visible_codes=[], selected_city=None, cities_on=False,
    ))
    assert check.error and "64" in check.error
    assert bot.set_calls == []


def test_saved_in_bot_is_applied_once_after_write(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, _Bot())
    asyncio.run(set_setting_by_admin(1, "bot_name", "Юлид’26 · регистрация"))
    assert bot.set_calls == ["Юлид’26 · регистрация"]
    assert bot.sent == []
    assert asyncio.run(db.get_setting("bot_name")) == "Юлид’26 · регистрация"


def test_same_name_is_not_resent(tmp_path, monkeypatch):
    """Повтор того же имени (и старт бота) не тратит редкий лимит Telegram."""
    bot = _ready(tmp_path, monkeypatch, _Bot(current="Юлид"))
    assert asyncio.run(bot_profile.apply_bot_name(bot, "Юлид")) is None
    assert asyncio.run(bot_profile.apply_bot_name(bot, "  ")) is None
    assert bot.set_calls == []


def test_refusal_in_bot_reverts_value_and_tells_admin(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, _Bot(fail=_retry(5400)))
    asyncio.run(db.set_setting("bot_name", "Старое имя"))
    asyncio.run(set_setting_by_admin(7, "bot_name", "Новое имя"))
    assert asyncio.run(db.get_setting("bot_name")) == "Старое имя"
    (chat_id, text), = bot.sent
    assert chat_id == 7 and "1 ч 30 мин" in text and "«Старое имя»" in text
    assert "Flood" not in text and "Retry" not in text


def test_refusal_without_previous_value_clears_setting(tmp_path, monkeypatch):
    bad = _Bot(fail=TelegramBadRequest(method=SetMyName(name="x"), message="Bad Request: name invalid"))
    _ready(tmp_path, monkeypatch, bad)
    asyncio.run(set_setting_by_admin(7, "bot_name", "Имя"))
    assert asyncio.run(db.get_setting("bot_name")) is None
    (_chat, text), = bad.sent
    assert "не принял" in text and "Bad Request" not in text


def test_bad_request_and_network_errors_are_human(tmp_path, monkeypatch):
    bad = _Bot(fail=TelegramBadRequest(method=SetMyName(name="x"), message="Bad Request: name invalid"))
    _ready(tmp_path, monkeypatch, bad)
    error = asyncio.run(bot_profile.apply_bot_name(bad, "Имя"))
    assert error and "не принял" in error and "Bad Request" not in error

    down = _Bot(fail=RuntimeError("proxy down"))
    error = asyncio.run(bot_profile.apply_bot_name(down, "Имя"))
    assert error and "через минуту" in error and "proxy" not in error


def test_app_process_save_does_not_call_telegram(tmp_path, monkeypatch):
    """Процесс приложения (бота нет): запись не зовёт Telegram — имя поставит бот из очереди."""
    _ready(tmp_path, monkeypatch, None)
    asyncio.run(set_setting_by_admin(1, "bot_name", "Имя"))
    assert asyncio.run(db.get_setting("bot_name")) == "Имя"


def test_app_save_is_applied_by_bot(tmp_path, monkeypatch):
    ok = _ready(tmp_path, monkeypatch, _Bot())
    asyncio.run(db.set_setting("bot_name", "Из приложения"))
    asyncio.run(miniapp_outbox._handle_row(
        ok, "settings_changed", {"keys": ["bot_name"], "by": 42, "prev_bot_name": "Было"}))
    assert ok.set_calls == ["Из приложения"] and ok.sent == []
    assert asyncio.run(db.get_setting("bot_name")) == "Из приложения"


def test_app_refusal_reverts_value_and_tells_author(tmp_path, monkeypatch):
    limited = _ready(tmp_path, monkeypatch, _Bot(fail=_retry(120)))
    asyncio.run(db.set_setting("bot_name", "Из приложения"))
    asyncio.run(miniapp_outbox._handle_row(
        limited, "settings_changed", {"keys": ["bot_name"], "by": 42, "prev_bot_name": "Было"}))
    assert asyncio.run(db.get_setting("bot_name")) == "Было"
    (chat_id, text), = limited.sent
    assert chat_id == 42 and "2 мин" in text and "не сменилось" in text


def test_newer_edit_is_not_overwritten_by_revert(tmp_path, monkeypatch):
    """Пока Telegram отвечал, имя успели поменять ещё раз — откат его не трогает."""
    bot = _ready(tmp_path, monkeypatch, _Bot(fail=_retry(60)))
    asyncio.run(db.set_setting("bot_name", "Попытка"))
    original_get = bot.get_my_name

    async def get_and_race():
        await db.set_setting("bot_name", "Ещё новее")
        return await original_get()

    bot.get_my_name = get_and_race
    asyncio.run(bot_profile.apply_saved_name(bot, "Было", 1))
    assert asyncio.run(db.get_setting("bot_name")) == "Ещё новее"


def test_startup_syncs_name_separately_from_description():
    import inspect
    import main

    src = inspect.getsource(main)
    i_profile = src.index("await sync_bot_profile(bot)")
    i_name = src.index("await sync_bot_name(bot)")
    between = src[i_profile:i_name]
    assert "except Exception" in between and "try:" in between
