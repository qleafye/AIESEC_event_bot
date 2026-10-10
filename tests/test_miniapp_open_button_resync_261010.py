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
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def set_chat_menu_button(self, menu_button=None, chat_id=None, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
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


def _save(key, value):
    async def go():
        await settings_audit.set_setting_by_admin(1, key, value)
        await asyncio.gather(*list(settings_audit._menu_resync_tasks))
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


def test_blocked_delegate_does_not_stop_the_others(monkeypatch, tmp_path):
    bot = FakeBot(fail_for={EN_DELEGATE})
    _ready(monkeypatch, tmp_path, bot)
    _save("miniapp_open_button", "📱 Приложение")
    assert set(_by_chat(bot)) == {None, RU_DELEGATE}


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


def test_manager_is_told_when_the_new_label_appears():
    note = settings_audit.after_save_note("miniapp_open_button")
    assert "в ближайшие минуты" in note and "на его языке" in note
    assert settings_audit.after_save_note("miniapp_open_text") == ""
