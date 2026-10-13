"""«🔒 Только менеджерам»: делегат не видит точек входа в приложение (кнопка меню чата,
пункт «📱 Приложение» в меню, кнопка «заполнить в приложении» в приветствии анкеты).

Хендлеры зовутся напрямую с фейками (pytest-asyncio в среде нет)."""
import asyncio

from aiogram.types import MenuButtonDefault, MenuButtonWebApp

from config import config
from database import db
from domain.settings.schema import get_setting_typed
from handlers import registration as reg
from handlers.i18n import reg_i18n
from handlers.settings import admin_miniapp
from keyboards import builders
from keyboards.builders import get_main_menu_kb
from tests._dbtpl import fast_init_db

ADMIN_ID = 261013001
MANAGER_ID = 261013002
DELEGATE_ID = 261013003


def _ready(tmp_path, *, enabled="on", staff_only="off"):
    config.DB_PATH = str(tmp_path / "staff_only_entry.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    config.DASHBOARD_PUBLIC_URL = "https://yl26.example.com"
    asyncio.run(db.set_setting("miniapp_enabled", enabled))
    asyncio.run(db.set_setting("miniapp_staff_only", staff_only))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))


class RecBot:
    def __init__(self, fail_for=()):
        self.calls = []  # (chat_id | None, button)
        self.fail_for = set(fail_for)

    async def set_chat_menu_button(self, menu_button=None, chat_id=None):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.calls.append((chat_id, menu_button))

    def last_for(self, chat_id):
        return [b for c, b in self.calls if c == chat_id][-1]


def test_off_behaviour_unchanged(tmp_path):
    _ready(tmp_path, staff_only="off")
    bot = RecBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert len(bot.calls) == 1
    assert bot.calls[0][0] is None and isinstance(bot.calls[0][1], MenuButtonWebApp)
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot, chat_id=DELEGATE_ID))
    assert isinstance(bot.last_for(DELEGATE_ID), MenuButtonWebApp)


def test_on_global_default_and_webapp_for_each_manager(tmp_path):
    _ready(tmp_path, staff_only="on")
    bot = RecBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert isinstance(bot.last_for(None), MenuButtonDefault)
    assert isinstance(bot.last_for(MANAGER_ID), MenuButtonWebApp)
    assert isinstance(bot.last_for(ADMIN_ID), MenuButtonWebApp)
    assert [c for c, _ in bot.calls].count(MANAGER_ID) == 1  # без дублей


def test_on_blocked_manager_does_not_stop_others(tmp_path):
    _ready(tmp_path, staff_only="on")
    bot = RecBot(fail_for={MANAGER_ID})
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert isinstance(bot.last_for(ADMIN_ID), MenuButtonWebApp)


def test_on_per_chat_delegate_default_manager_webapp(tmp_path):
    _ready(tmp_path, staff_only="on")
    asyncio.run(db.set_setting("role_reg_manager_enabled", "on"))
    bot = RecBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot, chat_id=DELEGATE_ID))
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot, chat_id=ADMIN_ID))
    assert isinstance(bot.last_for(DELEGATE_ID), MenuButtonDefault)
    assert isinstance(bot.last_for(ADMIN_ID), MenuButtonWebApp)


def test_enabled_off_resets_manager_buttons(tmp_path):
    _ready(tmp_path, enabled="off", staff_only="on")
    bot = RecBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    assert isinstance(bot.last_for(None), MenuButtonDefault)
    assert isinstance(bot.last_for(MANAGER_ID), MenuButtonDefault)
    assert isinstance(bot.last_for(ADMIN_ID), MenuButtonDefault)


def test_refresh_staff_button_only_when_staff_only_on(tmp_path):
    _ready(tmp_path, staff_only="off")
    bot = RecBot()
    asyncio.run(admin_miniapp.refresh_staff_menu_button(bot, MANAGER_ID))
    assert bot.calls == []
    asyncio.run(db.set_setting("miniapp_staff_only", "on"))
    asyncio.run(admin_miniapp.refresh_staff_menu_button(bot, DELEGATE_ID))
    assert isinstance(bot.last_for(DELEGATE_ID), MenuButtonDefault)


def _menu_texts():
    kb = asyncio.run(get_main_menu_kb(DELEGATE_ID))
    return [b.text for row in kb.keyboard for b in row]


def test_delegate_menu_hides_app_item_when_staff_only(tmp_path):
    _ready(tmp_path, staff_only="off")
    assert "📱 Приложение" in _menu_texts()
    asyncio.run(db.set_setting("miniapp_staff_only", "on"))
    assert "📱 Приложение" not in _menu_texts()


def test_hidden_reason_names_managers_only(tmp_path):
    _ready(tmp_path, staff_only="on")
    reason = asyncio.run(builders.menu_hidden_reason("menu_miniapp", None))
    assert reason == "приложение только для менеджеров — делегаты кнопку не видят"


class _Chat:
    id = DELEGATE_ID


class _Msg:
    chat = _Chat()


def test_reg_cta_none_when_staff_only(tmp_path, monkeypatch):
    _ready(tmp_path, staff_only="off")
    asyncio.run(db.set_setting("miniapp_section_form", "on"))

    async def _ctx(_t):
        return "ru", {}

    monkeypatch.setattr(reg_i18n, "ctx_for", _ctx)
    assert asyncio.run(reg._reg_form_cta_kb(_Msg())) is not None
    asyncio.run(db.set_setting("miniapp_staff_only", "on"))
    assert asyncio.run(reg._reg_form_cta_kb(_Msg())) is None


class _CbMsg:
    async def edit_text(self, *a, **k):
        pass


class _CbUser:
    id = ADMIN_ID


class _Cb:
    def __init__(self, bot):
        self.bot = bot
        self.from_user = _CbUser()
        self.message = _CbMsg()
        self.answers = []

    async def answer(self, text=None, **k):
        self.answers.append(text)


def test_toggle_staff_only_calls_sync(tmp_path):
    _ready(tmp_path, staff_only="off")
    bot = RecBot()
    cb = _Cb(bot)
    asyncio.run(admin_miniapp.toggle_miniapp_staff_only(cb))
    assert asyncio.run(get_setting_typed("miniapp_staff_only")) == "on"
    assert isinstance(bot.last_for(None), MenuButtonDefault)
    assert isinstance(bot.last_for(MANAGER_ID), MenuButtonWebApp)
