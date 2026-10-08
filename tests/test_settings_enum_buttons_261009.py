"""Enum-настройки в общем редакторе — кнопками с человеческими подписями, а не «напишите
forum/conference»."""
import asyncio
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from config import config
from handlers import admin_settings, admin_settings_enum
from handlers.admin_caps import required_capability
from handlers.states import EditSetting
from tests._dbtpl import fast_init_db

ADMIN = 900261009


def _run(coro):
    return asyncio.run(coro)


def test_event_type_screen_has_option_buttons(tmp_path):
    config.DB_PATH = str(tmp_path / "enum.db")
    fast_init_db()
    text, kb = _run(admin_settings._settings_edit_screen("event_type", None))
    labels = [row[0].text for row in kb.inline_keyboard]
    assert labels[:4] == ["Форум", "Конференция", "Вручную", "Форум СкиллАп"]
    assert kb.inline_keyboard[1][0].callback_data == "settings_enum_pick:1"
    assert "кнопкой" in text and "conference" not in text


def test_text_key_keeps_text_input(tmp_path):
    config.DB_PATH = str(tmp_path / "enum2.db")
    fast_init_db()
    text, kb = _run(admin_settings._settings_edit_screen("start_text", None))
    assert not any(b.callback_data.startswith("settings_enum_pick") for row in kb.inline_keyboard for b in row)
    assert "Пришлите новое значение" in text


def test_pick_routes_value_through_text_path(monkeypatch):
    seen = {}

    async def _fake_edit_value(message, state):
        seen["text"] = message.text
        seen["from"] = message.from_user.id

    monkeypatch.setattr(admin_settings, "settings_edit_value", _fake_edit_value)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    _run(state.set_state(EditSetting.waiting_for_value))
    _run(state.update_data(setting_key="event_type"))
    bot_msg = Message(message_id=5, date=datetime.now(), chat=Chat(id=ADMIN, type="private"),
                      from_user=User(id=1, is_bot=True, first_name="Бот"), text="экран")
    answers = []

    class _CB:
        data = "settings_enum_pick:1"
        from_user = User(id=ADMIN, is_bot=False, first_name="Админ")
        message = bot_msg
        bot = None

        async def answer(self, *a, **k):
            answers.append(a)

    _run(admin_settings_enum.settings_enum_pick(_CB(), state))
    assert seen == {"text": "conference", "from": ADMIN}
    assert required_capability(callback_data="settings_enum_pick:1") == "settings"
