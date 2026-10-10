"""Подпись кнопки приложения правится в боте (группа «📱 Приложение: тексты в чате»). Кнопку
меню чата Telegram держит до новой установки: общую — для всех, и свою — у каждого, кто выбрал
язык (`handlers/reg_lang.py`), своя главнее общей. После правки бот переставляет обе.

Синхронизацию не подменяем: подменён только Telegram (фейковый Bot записывает, какую кнопку и
какому чату поставили) — проверяется настоящий путь от записи настройки до кнопки чата."""
import asyncio

from aiogram.types import MenuButtonDefault, MenuButtonWebApp

import settings_audit
from config import config
from database import db
from tests._dbtpl import fast_init_db

EN_DELEGATE = 700001
RU_DELEGATE = 700002


class FakeBot:
    def __init__(self, fail_for=(), retry_after_once=()):
        self.calls = []
        self.fail_for = set(fail_for)
        self.retry_after_once = set(retry_after_once)

    async def set_chat_menu_button(self, menu_button=None, chat_id=None, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        if chat_id in self.retry_after_once:
            from aiogram.exceptions import TelegramRetryAfter
            from aiogram.methods import SetChatMenuButton

            self.retry_after_once.discard(chat_id)
            raise TelegramRetryAfter(SetChatMenuButton(), "Too Many Requests", 0)
        self.calls.append((chat_id, menu_button))


def _ready(monkeypatch, tmp_path, bot):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://example.test")
    fast_init_db()
    import services.scheduler as sch
    monkeypatch.setattr(sch, "_bot", bot)

    async def seed():
        await db.set_setting("miniapp_enabled", "on")
        await db.set_user_lang(EN_DELEGATE, "en")
        await db.set_user_lang(RU_DELEGATE, "ru")
    asyncio.run(seed())


def _save(*pairs):
    """Сохранения подряд (каждое — как из бота) и ожидание фонового прохода до конца."""
    async def go():
        for key, value in zip(pairs[::2], pairs[1::2]):
            await settings_audit.set_setting_by_admin(1, key, value)
        task = settings_audit._menu_resync_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(go())


def _by_chat(bot):
    return {chat_id: button for chat_id, button in bot.calls}


def test_new_label_reaches_common_button_and_every_delegate_with_language(monkeypatch, tmp_path):
    bot = FakeBot()
    _ready(monkeypatch, tmp_path, bot)
    _save("miniapp_open_button", "🚀 Открыть Юлид")
    buttons = _by_chat(bot)
    assert set(buttons) == {None, EN_DELEGATE, RU_DELEGATE}  # общая + своя у каждого с языком
    for button in buttons.values():
        assert isinstance(button, MenuButtonWebApp)
        assert button.text == "🚀 Открыть Юлид"
        assert button.web_app.url == "https://example.test/app"


def test_switching_app_off_removes_the_button_from_delegates_with_language(monkeypatch, tmp_path):
    bot = FakeBot()
    _ready(monkeypatch, tmp_path, bot)
    _save("miniapp_enabled", "off")
    buttons = _by_chat(bot)
    assert set(buttons) == {None, EN_DELEGATE, RU_DELEGATE}
    assert all(isinstance(b, MenuButtonDefault) for b in buttons.values())


def test_blocked_delegate_does_not_stop_the_others(monkeypatch, tmp_path, caplog):
    bot = FakeBot(fail_for={EN_DELEGATE})
    _ready(monkeypatch, tmp_path, bot)
    with caplog.at_level("WARNING"):
        _save("miniapp_open_button", "📱 Приложение")
    assert set(_by_chat(bot)) == {None, RU_DELEGATE}
    assert any(str(EN_DELEGATE) in r.getMessage() for r in caplog.records if r.levelname == "WARNING")


def test_too_many_requests_waits_and_retries(monkeypatch, tmp_path):
    bot = FakeBot(retry_after_once={EN_DELEGATE})
    _ready(monkeypatch, tmp_path, bot)
    _save("miniapp_open_button", "📱 Приложение")
    assert _by_chat(bot)[EN_DELEGATE].text == "📱 Приложение"


def test_second_save_restarts_the_pass_and_the_last_label_wins(monkeypatch, tmp_path):
    """Один проход на процесс: второе сохранение отменяет первый проход, а не идёт рядом."""
    bot = FakeBot()
    _ready(monkeypatch, tmp_path, bot)

    async def two_saves():
        await settings_audit.set_setting_by_admin(1, "miniapp_open_button", "Первая")
        first = settings_audit._menu_resync_task
        await settings_audit.set_setting_by_admin(1, "miniapp_open_button", "Вторая")
        second = settings_audit._menu_resync_task
        await second
        return first, second
    first, second = asyncio.run(two_saves())
    assert first is not second and first.cancelled()
    assert {b.text for b in _by_chat(bot).values()} == {"Вторая"}


def test_other_keys_do_not_touch_chat_menu_button(monkeypatch, tmp_path):
    bot = FakeBot()
    _ready(monkeypatch, tmp_path, bot)
    _save("miniapp_open_text", "Привет")
    assert bot.calls == []


def test_process_without_bot_saves_quietly(monkeypatch, tmp_path, caplog):
    _ready(monkeypatch, tmp_path, None)
    import services.scheduler as sch
    monkeypatch.setattr(sch, "_bot", None)
    with caplog.at_level("INFO"):
        _save("miniapp_open_button", "📱 Приложение")
    assert asyncio.run(db.get_setting("miniapp_open_button")) == "📱 Приложение"
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_manager_is_told_whether_the_button_is_being_updated(monkeypatch, tmp_path):
    bot = FakeBot()
    _ready(monkeypatch, tmp_path, bot)

    async def save_and_note():
        await settings_audit.set_setting_by_admin(1, "miniapp_open_button", "📱")
        note = settings_audit.after_save_note("miniapp_open_button")
        await settings_audit._menu_resync_task
        return note
    assert "в ближайшие минуты" in asyncio.run(save_and_note())
    assert settings_audit.after_save_note("miniapp_open_text") == ""

    import services.scheduler as sch
    monkeypatch.setattr(sch, "_bot", None)
    monkeypatch.setattr(settings_audit, "_menu_resync_task", None)

    async def save_without_bot():
        await settings_audit.set_setting_by_admin(1, "miniapp_open_button", "📱")
        return settings_audit.after_save_note("miniapp_open_button")
    assert "не удалось" in asyncio.run(save_without_bot())
