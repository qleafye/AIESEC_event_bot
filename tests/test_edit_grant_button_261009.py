"""«✏️ Открыта правка после решения»: делегат получает кнопку правки в чате (/start edit), а не
отсылку в профиль приложения, которого на событии может не быть."""
import asyncio

from config import config
from handlers import admin_edit_grant
from services import quiet_hours
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db


class _Me:
    username = "YouLead_test_bot"


class _Bot:
    def __init__(self):
        self.sent = []

    async def me(self):
        return _Me()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def test_notify_has_edit_deeplink_button(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "editg.db")
    fast_init_db()
    queued = {}

    async def _send_now(now, user_id, text, *, sender, parse_mode="HTML", reply_markup=None):
        queued["markup"] = reply_markup
        await sender()
        return True

    monkeypatch.setattr(quiet_hours, "send_or_queue_text", _send_now)
    bot = _Bot()
    assert asyncio.run(admin_edit_grant._notify_delegate(bot, 5551, None)) is True
    (_, text, kb), = bot.sent
    button = kb.inline_keyboard[0][0]
    assert button.url == "https://t.me/YouLead_test_bot?start=edit"
    assert queued["markup"] is kb  # утренняя доставка из тихих часов тоже несёт кнопку
    assert "в профиле" not in SETTINGS_SCHEMA["edit_granted_notify_text"]["default"]
