"""Идея №3 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): приветствие делегату
после ПЕРВОЙ отметки входа (`services/forum_welcome.py`, слушатель `services.checkin.
register_first_entry_listener`).

pytest-asyncio недоступна в этом окружении — каждый async-вызов через `asyncio.run()`,
`config.DB_PATH` указывает на файл в `tmp_path`, БД — `tests/_dbtpl.py::fast_init_db` (та же
форма, что `tests/test_checkin_first_entry_hook_260924.py`, откуда позаимствован приём
регистрации/очистки слушателей)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
import services.checkin as checkin_mod
from services.checkin import ENTRY_POINT, record_arrival
import services.forum_welcome as fw
from tests._dbtpl import fast_init_db
from tests._lang_on import enable_delegate_lang

ADMIN_ID = 924001
UID = 924010


def _ready(tmp_path, name="test_forum_welcome_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _enable_cities():
    _run(db.set_setting("event_city_enabled", "on"))


async def _add_delegate(tid: int, event_city: str | None = None):
    await db.add_user({
        "telegram_id": tid, "full_name": "Тест Делегатов", "username": "testdel",
        "event_city": event_city, "registration_date": "2026-09-01 00:00:00",
        "status": "approved",
    })


class FakeBot:
    def __init__(self, *, raise_on_send: bool = False):
        self.sent: list[tuple[int, str]] = []
        self._raise = raise_on_send

    async def send_message(self, chat_id, text, **kwargs):
        if self._raise:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text))


def _clean():
    checkin_mod.clear_first_entry_listeners()


def setup_function(_):
    _clean()


def teardown_function(_):
    _clean()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Чистые функции: _is_fresh_csv_scan / _should_send / _format_time
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_is_fresh_csv_scan_true_within_hour_today(monkeypatch):
    now = datetime(2026, 10, 30, 10, 0, 0)
    monkeypatch.setattr("services.timeutil.msk_now", lambda: now)
    assert fw._is_fresh_csv_scan("2026-10-30 09:15:00", False) is True


def test_is_fresh_csv_scan_false_older_than_hour(monkeypatch):
    now = datetime(2026, 10, 30, 10, 0, 0)
    monkeypatch.setattr("services.timeutil.msk_now", lambda: now)
    assert fw._is_fresh_csv_scan("2026-10-30 08:59:00", False) is False


def test_is_fresh_csv_scan_false_different_day(monkeypatch):
    now = datetime(2026, 10, 30, 10, 0, 0)
    monkeypatch.setattr("services.timeutil.msk_now", lambda: now)
    assert fw._is_fresh_csv_scan("2026-10-29 09:50:00", False) is False


def test_is_fresh_csv_scan_false_when_approx(monkeypatch):
    now = datetime(2026, 10, 30, 10, 0, 0)
    monkeypatch.setattr("services.timeutil.msk_now", lambda: now)
    assert fw._is_fresh_csv_scan("2026-10-30 09:59:00", True) is False


def test_is_fresh_csv_scan_false_empty_or_garbage(monkeypatch):
    now = datetime(2026, 10, 30, 10, 0, 0)
    monkeypatch.setattr("services.timeutil.msk_now", lambda: now)
    assert fw._is_fresh_csv_scan(None, False) is False
    assert fw._is_fresh_csv_scan("не время", False) is False


def test_should_send_live_sources_always_true():
    assert fw._should_send("miniapp", None, False) is True
    assert fw._should_send("manual", None, False) is True
    assert fw._should_send("auto_session", None, False) is True


def test_should_send_csv_delegates_to_freshness(monkeypatch):
    now = datetime(2026, 10, 30, 10, 0, 0)
    monkeypatch.setattr("services.timeutil.msk_now", lambda: now)
    assert fw._should_send("csv", "2026-10-30 09:59:00", False) is True
    assert fw._should_send("csv", "2026-10-29 09:59:00", False) is False


def test_format_time_extracts_hhmm():
    assert fw._format_time("2026-10-30 09:15:00") == "09:15"


def test_format_time_falls_back_on_garbage():
    assert fw._format_time(None) == "сейчас"
    assert fw._format_time("") == "сейчас"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр: дефолты/формат/TOGGLE_SECTION
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_registry_defaults_and_format():
    from domain.settings.schema import SETTINGS_SCHEMA
    import domain.settings.ops as settings_ops

    enabled = SETTINGS_SCHEMA["forum_welcome_enabled"]
    assert enabled["default"] == "off"
    assert enabled["per_city"] is True
    assert enabled["type"] == "enum"

    text = SETTINGS_SCHEMA["forum_welcome_text"]
    assert text["type"] == "text"
    assert text["per_city"] is True
    assert "{time}" in text["default"]

    assert settings_ops.TOGGLE_SECTION["forum_welcome_enabled"] == "apps"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Интеграция: register() + record_arrival -> bot.send_message
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_no_send_when_toggle_off(tmp_path):
    _ready(tmp_path)
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(_run(db.get_user(UID)), ENTRY_POINT, source="miniapp", bot=bot))
    assert bot.sent == []


def test_sends_on_live_source_with_time_substituted(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(
        _run(db.get_user(UID)), ENTRY_POINT, source="miniapp",
        scanned_at="2026-10-30 09:15:00", bot=bot,
    ))
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == UID
    assert "09:15" in text
    assert "{time}" not in text


def test_manual_and_auto_session_sources_also_send(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(_run(db.get_user(UID)), ENTRY_POINT, source="manual", by_staff_id=5, bot=bot))
    assert len(bot.sent) == 1


def test_duplicate_entry_does_not_resend(tmp_path):
    """Хук `services.checkin.record_arrival` зовёт слушателей только на ПЕРВОЙ отметке —
    повторный скан молчит (та же гарантия, что `tests/test_checkin_first_entry_hook_260924.py`
    уже проверяет для самого хука)."""
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    user = _run(db.get_user(UID))
    _run(record_arrival(user, ENTRY_POINT, source="miniapp", bot=bot))
    _run(record_arrival(user, ENTRY_POINT, source="miniapp", bot=bot))
    assert len(bot.sent) == 1


def test_second_day_of_forum_does_not_resend(tmp_path):
    """Вход каждый день: первый вход ВТОРОГО дня двухдневного форума зовёт хук ещё раз, но с
    `first_of_forum=False` — приветствие уходит только на первый день, повторно не шлётся."""
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    user = _run(db.get_user(UID))
    _run(record_arrival(user, ENTRY_POINT, source="miniapp", bot=bot))
    assert len(bot.sent) == 1
    # Второй день -- имитируем сменой даты в самой отметке (UNIQUE по дню, "новый" вход).
    from datetime import timedelta

    from services.timeutil import msk_now

    tomorrow = (msk_now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    _run(record_arrival(user, ENTRY_POINT, source="miniapp", scanned_at=tomorrow, bot=bot))
    assert len(bot.sent) == 1  # второй день форума -- приветствие не ушло повторно


def test_first_day_of_forum_sends(tmp_path):
    """Первый вход первого дня форума -- `first_of_forum=True` -- приветствие уходит (базовый
    случай, зафиксирован отдельно от `test_second_day_of_forum_does_not_resend` для ясности
    контраста)."""
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(
        _run(db.get_user(UID)), ENTRY_POINT, source="miniapp",
        scanned_at="2026-10-30 09:15:00", bot=bot,
    ))
    assert len(bot.sent) == 1


def test_missing_first_of_forum_kwarg_defaults_to_send(tmp_path):
    """Обратная совместимость: если вызывающий не передал `first_of_forum` вообще (старое
    событие из outbox), слушатель трактует это как True и шлёт приветствие."""
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(fw._on_first_entry(bot, UID, "msk", "2026-10-30", source="miniapp", scanned_at="2026-10-30 09:15:00"))
    assert len(bot.sent) == 1


def test_csv_source_fresh_scan_sends(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    from services import timeutil

    # «Сейчас» — полдень: прогон около полуночи по МСК уводил скан «10 минут назад» во
    # вчерашний день, и первым входом форума он уже не считался.
    noon = timeutil.msk_now().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(timeutil, "msk_now", lambda: noon)
    now = noon
    fresh = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    bot = FakeBot()
    _run(record_arrival(
        _run(db.get_user(UID)), ENTRY_POINT, source="csv", scanned_at=fresh, bot=bot,
    ))
    assert len(bot.sent) == 1


def test_csv_source_stale_scan_does_not_send(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(
        _run(db.get_user(UID)), ENTRY_POINT, source="csv",
        scanned_at="2020-01-01 08:00:00", bot=bot,
    ))
    assert bot.sent == []


def test_csv_source_approx_time_does_not_send(tmp_path):
    """`approx=True` -- «время скана не нашлось в файле, подставлено время загрузки» -- молчим,
    даже если формально «сегодня и только что» (D-10)."""
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    from services.timeutil import msk_now

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    bot = FakeBot()
    _run(record_arrival(
        _run(db.get_user(UID)), ENTRY_POINT, source="csv", scanned_at=now, approx=True, bot=bot,
    ))
    assert bot.sent == []


def test_empty_text_does_not_send(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    _run(db.set_setting("forum_welcome_text", ""))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(_run(db.get_user(UID)), ENTRY_POINT, source="miniapp", bot=bot))
    assert bot.sent == []


def test_send_failure_is_fail_soft(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot(raise_on_send=True)
    result = _run(record_arrival(_run(db.get_user(UID)), ENTRY_POINT, source="miniapp", bot=bot))
    assert result["status"] == "new"  # отметка сохранена, несмотря на сбой отправки


def test_percity_spb_on_msk_off(tmp_path):
    _ready(tmp_path)
    _enable_cities()
    _run(db.set_setting("forum_welcome_enabled__city__spb", "on"))
    fw.register()
    _run(_add_delegate(924011, event_city="spb"))
    _run(_add_delegate(924012, event_city="msk"))
    bot = FakeBot()
    _run(record_arrival(_run(db.get_user(924011)), ENTRY_POINT, source="miniapp", bot=bot))
    _run(record_arrival(_run(db.get_user(924012)), ENTRY_POINT, source="miniapp", bot=bot))
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 924011


def test_register_is_idempotent(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    fw.register()
    fw.register()
    fw.register()
    _run(_add_delegate(UID))
    bot = FakeBot()
    _run(record_arrival(_run(db.get_user(UID)), ENTRY_POINT, source="miniapp", bot=bot))
    assert len(bot.sent) == 1  # не утроилось от трёх регистраций


def test_english_delegate_gets_translated_greeting(tmp_path):
    """Задача 3 (D-03 соседей): текст уходит через тот же перевод, что «🎟 Мой QR» —
    `handlers.i18n.reg_i18n.tr_fmt`, шаблон переводится СНАЧАЛА (`services.i18n_form_manual.
    FORM_DEFAULT_EN`), `{time}` подставляется ПОСЛЕ (тот же приём, что
    `tests/test_checkin_qr_broadcast_260924.py::test_send_broadcast_translates_caption_for_english_delegate`)."""
    import sqlite3
    from services.i18n_form_manual import FORM_DEFAULT_EN, seed

    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    _run(enable_delegate_lang())
    _run(seed())
    fw.register()
    _run(_add_delegate(UID))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET lang = 'en' WHERE telegram_id = ?", (UID,))
    conn.commit()
    conn.close()

    bot = FakeBot()
    _run(record_arrival(
        _run(db.get_user(UID)), ENTRY_POINT, source="miniapp",
        scanned_at="2026-10-30 09:15:00", bot=bot,
    ))
    assert len(bot.sent) == 1
    default_ru = "Ты отмечен на входе в {time} ✅ Добро пожаловать на {event}!"
    expected = FORM_DEFAULT_EN[default_ru].replace("{time}", "09:15").replace("{event}", "the event")
    assert bot.sent[0][1] == expected


# ══════════════════════════════════════════════════════════════════════════════════════════
# Хаб «🎪 Форум: функции» — строка + свой экран (тумблер-only)
# ══════════════════════════════════════════════════════════════════════════════════════════

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, user_id=ADMIN_ID):
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_hub_shows_welcome_row_and_button(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert "Приветствие после отметки на входе" in text
    assert "forumwelcome_cfg:msk" in _cbs(kb)


def test_cfg_screen_toggle_flips_global_setting(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    callback = _FakeCallback("forumwelcome_toggle:_all")
    _run(aff.forumwelcome_toggle_go(callback))
    assert _run(db.get_setting("forum_welcome_enabled")) == "on"
    callback2 = _FakeCallback("forumwelcome_toggle:_all")
    _run(aff.forumwelcome_toggle_go(callback2))
    assert _run(db.get_setting("forum_welcome_enabled")) == "off"


def test_cfg_screen_writes_percity_key_when_module_on(tmp_path):
    from handlers import admin_forum_functions as aff
    from handlers.access.admin_caps import role_caps_key

    _ready(tmp_path)
    _enable_cities()
    _run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))
    _run(db.add_staff(924099, "reg_manager", ADMIN_ID))
    _run(db.set_staff_city(924099, "spb"))
    callback = _FakeCallback("forumwelcome_toggle:spb", user_id=924099)
    _run(aff.forumwelcome_toggle_go(callback))
    assert _run(db.get_setting("forum_welcome_enabled__city__spb")) == "on"
    assert _run(db.get_setting("forum_welcome_enabled")) is None


def test_cfg_screen_warns_when_text_empty(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    _run(db.set_setting("forum_welcome_text", ""))
    text, _kb = _run(aff._welcome_cfg_text_kb(None))
    assert "пуст" in text
