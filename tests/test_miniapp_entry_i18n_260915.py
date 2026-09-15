"""Квик 260915-skg, Задача 4 (P7): вход в приложение по-английски при lang=en.

Reply-кнопка меню «📱 Приложение»/«📱 App» уже переводится (`keyboards/builders.py::MENU_EN`,
квик 260912) — непереведённым оставался ОТВЕТ хендлера `handlers/user_actions.py::
open_miniapp_button` (`miniapp_open_text`/`miniapp_open_button`/`miniapp_disabled_text` уходили
сырым `message.answer` мимо `reg_i18n`). Форма теста — по образцу
`tests/test_menu_i18n_260912.py`: реальный хендлер, фейковый `Message`, `asyncio.run()`
(pytest-asyncio в окружении нет).
"""
import asyncio

from aiogram.types import WebAppInfo

from config import config
from database import db

from handlers import user_actions as ua_mod

UID = 813915


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text, chat_id=UID):
        self.text = text
        self.chat = _FakeChat(chat_id)
        self.from_user = _FakeUser(chat_id)
        self.calls = []

    async def answer(self, text=None, **kwargs):
        self.calls.append((text, kwargs))
        return "sent"


def _use_tmp_db(tmp_path, name="test_miniapp_entry_i18n_260915.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _enable_lang_module():
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_enabled", "on"),
        )
        await conn.commit()


async def _enable_miniapp():
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("miniapp_enabled", "on"),
        )
        await conn.commit()


async def _seed_user(lang):
    await db.add_user({"telegram_id": UID, "full_name": "Delegate", "registration_date": None})
    await db.set_user_lang(UID, lang)


# ── lang=en: текст сообщения и подпись inline-кнопки английские, web_app.url не тронут ─────

def test_english_delegate_gets_english_text_and_button(tmp_path):
    _use_tmp_db(tmp_path)
    config.DASHBOARD_PUBLIC_URL = "https://example.aiesec-bot.test"

    async def go():
        await _enable_lang_module()
        await _enable_miniapp()
        await _seed_user("en")
        message = _FakeMessage("📱 App")
        await ua_mod.open_miniapp_button(message)
        return message.calls

    calls = asyncio.run(go())
    assert len(calls) == 1
    text, kwargs = calls[0]
    assert text == "Tasks, coins and leaderboard in one place. Open the app 👇"
    kb = kwargs["reply_markup"]
    btn = kb.inline_keyboard[0][0]
    assert btn.text == "📱 Open the app"
    assert isinstance(btn.web_app, WebAppInfo)
    assert btn.web_app.url == "https://example.aiesec-bot.test/app"


# ── lang=en, приложение выключено: английский miniapp_disabled_text ────────────────────────

def test_english_delegate_gets_english_disabled_text_when_app_off(tmp_path):
    _use_tmp_db(tmp_path, name="test_miniapp_entry_i18n_260915_off.db")
    config.DASHBOARD_PUBLIC_URL = "https://example.aiesec-bot.test"

    async def go():
        await _enable_lang_module()
        # miniapp_enabled НЕ включаем -- дефолт "off" (settings_schema.py).
        await _seed_user("en")
        message = _FakeMessage("📱 App")
        await ua_mod.open_miniapp_button(message)
        return message.calls

    calls = asyncio.run(go())
    assert len(calls) == 1
    text, _kwargs = calls[0]
    assert text == "The app is temporarily unavailable. Everything is also here in the bot."


# ── lang=ru: байт-в-байт прежние тексты реестра ─────────────────────────────────────────────

def test_russian_delegate_gets_unchanged_russian_text_and_button(tmp_path):
    _use_tmp_db(tmp_path, name="test_miniapp_entry_i18n_260915_ru.db")
    config.DASHBOARD_PUBLIC_URL = "https://example.aiesec-bot.test"

    async def go():
        await _enable_lang_module()
        await _enable_miniapp()
        await _seed_user("ru")
        message = _FakeMessage("📱 Приложение")
        await ua_mod.open_miniapp_button(message)
        return message.calls

    calls = asyncio.run(go())
    assert len(calls) == 1
    text, kwargs = calls[0]
    assert text == "Задания, монеты и рейтинг — в одном экране. Открывай приложение 👇"
    btn = kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.text == "📱 Открыть приложение"


# ── менеджер переопределил текст своим русским -- общий путь перевода, не копия словаря ────

def test_manager_override_text_still_goes_through_general_translation_path(tmp_path):
    _use_tmp_db(tmp_path, name="test_miniapp_entry_i18n_260915_override.db")
    config.DASHBOARD_PUBLIC_URL = "https://example.aiesec-bot.test"

    async def go():
        await _enable_lang_module()
        await _enable_miniapp()
        await db.set_setting("miniapp_open_text", "Свой текст менеджера про приложение")
        await _seed_user("en")
        message = _FakeMessage("📱 App")
        await ua_mod.open_miniapp_button(message)
        return message.calls

    calls = asyncio.run(go())
    assert len(calls) == 1
    text, _kwargs = calls[0]
    # Нет записи в UI_EN/tr_map для кастомного текста менеджера -- fail-soft отдаёт русский
    # как есть (services.i18n.tr, ярус A -> tr_map -> русский), хендлер не заводит свою копию.
    assert text == "Свой текст менеджера про приложение"


# ── структурная проверка: ни одного message.answer без tr_text в open_miniapp_button ────────

def test_open_miniapp_button_has_no_raw_message_answer_without_tr_text():
    import inspect
    import re

    source = inspect.getsource(ua_mod.open_miniapp_button)
    for m in re.finditer(r"message\.answer\(([^)]*)\)", source):
        arg = m.group(1).split(",")[0].strip()
        assert arg == "text", f"message.answer вызван не через переменную text: {m.group(0)!r}"
