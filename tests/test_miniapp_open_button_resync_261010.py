"""Подпись кнопки приложения правится в боте (группа «📱 Приложение: тексты в чате») — после
записи бот переставляет кнопку меню чата, иначе там до перезапуска висела бы старая подпись."""
import asyncio

import settings_audit
from config import config
from tests._dbtpl import fast_init_db


def _hooks_calls(monkeypatch, tmp_path, key):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    fast_init_db()
    calls = []

    async def fake_sync(bot, chat_id=None, lang="ru"):
        calls.append(bot)

    import handlers.admin_miniapp as am
    import services.scheduler as sch
    monkeypatch.setattr(am, "sync_chat_menu_button", fake_sync)
    monkeypatch.setattr(sch, "get_bot", lambda: "BOT")
    asyncio.run(settings_audit.run_setting_hooks(key))
    return calls


def test_open_button_edit_resyncs_chat_menu_button(monkeypatch, tmp_path):
    assert _hooks_calls(monkeypatch, tmp_path, "miniapp_open_button") == ["BOT"]


def test_other_keys_do_not_touch_chat_menu_button(monkeypatch, tmp_path):
    assert _hooks_calls(monkeypatch, tmp_path, "miniapp_open_text") == []


def test_unavailable_bot_does_not_break_the_edit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    fast_init_db()
    import services.scheduler as sch

    def boom():
        raise RuntimeError("Scheduler not initialised")

    monkeypatch.setattr(sch, "get_bot", boom)
    asyncio.run(settings_audit.run_setting_hooks("miniapp_open_button"))  # не падает
