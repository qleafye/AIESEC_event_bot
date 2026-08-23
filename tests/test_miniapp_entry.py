"""Phase 19 (08, D-10) — точки входа Mini App: текстовая reply-кнопка «📱 Приложение» +
inline web_app-кнопка + кнопка меню чата (setChatMenuButton).

Handlers called DIRECTLY with Fake message/callback/bot doubles (same convention as every
other phase-9/16/19 test file — pytest-asyncio unavailable in this env).

Pitfall 1 (RESEARCH): a reply KeyboardButton(web_app=...) gives a "simple web view" with an
EMPTY initData — the delegate would never authenticate. The reply button MUST stay textual;
only the inline button (sent by the handler) and setChatMenuButton carry a full initData.
"""
import asyncio
import inspect

from config import config
from database import db
from handlers import user_actions as ua_mod
from handlers import admin_miniapp
from keyboards.builders import get_main_menu_kb, MENU_BUTTONS
from aiogram.types import InlineKeyboardButton, MenuButtonDefault, MenuButtonWebApp


ADMIN_ID = 931101
DELEGATE_ID = 931102


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_miniapp_entry.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    config.DASHBOARD_PUBLIC_URL = "https://yl26.example.com"


def _enable_miniapp():
    asyncio.run(db.set_setting("miniapp_enabled", "on"))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.menu_button_calls = []

    async def set_chat_menu_button(self, menu_button=None):
        if self.fail:
            raise RuntimeError("Telegram unreachable")
        self.menu_button_calls.append(menu_button)


def _flat_reply_texts(kb):
    return [btn.text for row in kb.keyboard for btn in row]


# ── reply-кнопка меню (keyboards/builders.py) ───────────────────────────────────────────

def test_menu_miniapp_button_shown_when_enabled_and_url_set(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    kb = asyncio.run(get_main_menu_kb())
    assert "📱 Приложение" in _flat_reply_texts(kb)


def test_menu_miniapp_button_hidden_when_disabled(tmp_path):
    _db_ready(tmp_path)
    # miniapp_enabled defaults to "off"
    kb = asyncio.run(get_main_menu_kb())
    assert "📱 Приложение" not in _flat_reply_texts(kb)


def test_menu_miniapp_button_hidden_when_url_empty(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    config.DASHBOARD_PUBLIC_URL = ""
    kb = asyncio.run(get_main_menu_kb())
    assert "📱 Приложение" not in _flat_reply_texts(kb)


def test_menu_miniapp_button_hidden_when_menu_toggle_off_even_if_app_enabled(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    asyncio.run(db.set_setting("menu_miniapp", "off"))
    kb = asyncio.run(get_main_menu_kb())
    assert "📱 Приложение" not in _flat_reply_texts(kb)


def test_other_menu_buttons_untouched_by_miniapp_gate(tmp_path):
    _db_ready(tmp_path)
    # miniapp off (default) must not hide any other button
    kb = asyncio.run(get_main_menu_kb())
    texts = _flat_reply_texts(kb)
    assert "🎯 Задания" in texts
    assert "🪙 Мои монеты" in texts


def test_menu_miniapp_registered_right_after_game_tasks():
    keys = [k for k, _ in MENU_BUTTONS]
    assert keys.index("menu_miniapp") == keys.index("menu_game_tasks") + 1


# ── структурный тест: reply-кнопка НЕ web_app (Pitfall 1) ──────────────────────────────

def test_reply_menu_never_builds_a_webapp_keyboard_button():
    """MENU_BUTTONS + get_main_menu_kb build a ReplyKeyboardBuilder purely from (key, text)
    pairs via kb.button(text=text) -- no `web_app=` kwarg anywhere in the function body. A
    literal source check closes the door on someone "fixing" the reply button into a
    KeyboardButton(web_app=...) later (Pitfall 1 would silently return)."""
    source = inspect.getsource(get_main_menu_kb)
    assert "web_app" not in source


# ── хендлер текстовой кнопки → inline web_app ───────────────────────────────────────────

def test_open_miniapp_button_sends_inline_webapp_button(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    message = FakeMessage(text="📱 Приложение")
    asyncio.run(ua_mod.open_miniapp_button(message))
    assert len(message.answers) == 1
    text, parse_mode, kb = message.answers[0]
    assert kb is not None
    row = kb.inline_keyboard[0]
    btn = row[0]
    assert isinstance(btn, InlineKeyboardButton)
    assert btn.web_app is not None
    assert btn.web_app.url.endswith("/app")
    assert btn.web_app.url.startswith(config.DASHBOARD_PUBLIC_URL)


def test_open_miniapp_button_disabled_explains_without_kb(tmp_path):
    _db_ready(tmp_path)
    # miniapp_enabled defaults to "off"
    message = FakeMessage(text="📱 Приложение")
    asyncio.run(ua_mod.open_miniapp_button(message))
    assert len(message.answers) == 1
    text, parse_mode, kb = message.answers[0]
    assert kb is None
    assert text  # human explanation from the registry


def test_open_miniapp_button_empty_url_explains_without_kb(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    config.DASHBOARD_PUBLIC_URL = ""
    message = FakeMessage(text="📱 Приложение")
    asyncio.run(ua_mod.open_miniapp_button(message))
    text, parse_mode, kb = message.answers[0]
    assert kb is None


# ── setChatMenuButton (main.py startup + toggle handler) ────────────────────────────────

def test_sync_chat_menu_button_sets_webapp_when_enabled(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    bot = FakeBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert len(bot.menu_button_calls) == 1
    assert isinstance(bot.menu_button_calls[0], MenuButtonWebApp)
    assert bot.menu_button_calls[0].web_app.url.endswith("/app")


def test_sync_chat_menu_button_sets_default_when_disabled(tmp_path):
    _db_ready(tmp_path)
    # miniapp_enabled defaults to "off"
    bot = FakeBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert isinstance(bot.menu_button_calls[0], MenuButtonDefault)


def test_sync_chat_menu_button_sets_default_when_url_empty(tmp_path):
    _db_ready(tmp_path)
    _enable_miniapp()
    config.DASHBOARD_PUBLIC_URL = ""
    bot = FakeBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert isinstance(bot.menu_button_calls[0], MenuButtonDefault)
