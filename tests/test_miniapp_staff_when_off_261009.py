"""Выключенное приложение закрыто делегатам, не персоналу.

Тумблер «📱 Приложение включено» = off (дефолт): делегат видит `miniapp_disabled_text`, как
раньше, а менеджеры входят — сканер, мастер первой настройки и поиск настроек есть только в
приложении. Веб: `miniapp/main.py::_passes_when_off`; бот: `services/miniapp_access.py`
(меню «📱 Приложение», его хендлер, кнопка «📱 Приложение в браузере»).
"""
import asyncio

import pytest
from aiogram.types import WebAppInfo

from config import config
from database import db
from handlers.settings import admin_miniapp
from handlers import user_actions as ua_mod
from keyboards import builders
from miniapp.file_tokens import mint_file_token
from services import miniapp_access

from tests.test_miniapp_auth import TOKEN
from tests.test_miniapp_routes import (
    ADMIN_ID, DELEGATE_ID, GAME_MANAGER_ID, _cfg, _client, _hdr, _seed, _set, _standard_seed,
    _use_tmp_db,
)


@pytest.fixture
def off_client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_staff_off.db")
    _standard_seed()
    _set("miniapp_enabled", "off")
    return _client(_cfg(db_path))


# ── Веб-гейт ─────────────────────────────────────────────────────────────────────────────

def test_superadmin_and_staff_enter_when_off(off_client):
    assert off_client.get("/app/api/me", headers=_hdr(ADMIN_ID)).status_code == 200
    assert off_client.get("/app/api/me", headers=_hdr(GAME_MANAGER_ID)).status_code == 200


def test_delegate_gets_miniapp_off_when_off(off_client):
    resp = off_client.get("/app/api/me", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 503
    assert resp.json() == {"reason": "miniapp_off"}


def test_shell_is_served_when_off_so_staff_can_load_the_app(off_client):
    resp = off_client.get("/app")
    assert resp.status_code == 200
    assert "/js/app.js" in resp.text
    # Делегат увидит этот текст: клиент рисует его на 503 miniapp_off.
    assert 'data-disabled-text="' in resp.text


def test_anonymous_api_still_closed_when_off(off_client):
    resp = off_client.get("/app/api/me")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "no_auth"


def test_bad_initdata_is_401_not_a_pass(off_client):
    resp = off_client.get("/app/api/me", headers={"X-Telegram-Init-Data": "user=%7B%7D&hash=bad"})
    assert resp.status_code == 401


def test_file_token_of_staff_passes_delegate_does_not(off_client):
    staff_t = mint_file_token(TOKEN, ADMIN_ID)
    delegate_t = mint_file_token(TOKEN, DELEGATE_ID)
    # Неверный формат file_id — маршрут ответит 404, не ходя в Telegram; важно, что гейт пропустил.
    assert off_client.get("/app/api/file/!bad", params={"t": staff_t}).status_code == 404
    resp = off_client.get("/app/api/file/!bad", params={"t": delegate_t})
    assert resp.status_code == 503 and resp.json() == {"reason": "miniapp_off"}


def test_bot_only_role_is_not_staff_of_the_app(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_staff_off_marketing.db")
    _standard_seed()
    _set("miniapp_enabled", "off")
    marketer = 900777
    _seed(staff=[(marketer, "marketing_manager", None)])  # роль по умолчанию — только source_links
    resp = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(marketer))
    assert resp.status_code == 503


def test_unreadable_db_keeps_human_disabled_page(off_client, monkeypatch):
    import miniapp.main as main_mod

    monkeypatch.setattr(main_mod, "_miniapp_state", lambda db_path: "error")
    resp = off_client.get("/app")
    assert resp.status_code == 503
    assert "text/html" in resp.headers["content-type"]
    assert off_client.get("/app/api/me", headers=_hdr(ADMIN_ID)).status_code == 503


def test_health_and_on_state_unchanged(off_client):
    assert off_client.get("/app/health").status_code == 200
    _set("miniapp_enabled", "on")
    assert off_client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).status_code == 200


# ── Бот: точки входа ─────────────────────────────────────────────────────────────────────

ADMIN = 920001
PLAIN = 920002


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "staff_off_bot.db"))
    from tests._dbtpl import fast_init_db
    fast_init_db()
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN])
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl.example.test")


def test_miniapp_open_for(bot_env):
    async def go():
        assert await miniapp_access.miniapp_open_for(ADMIN) is True
        assert await miniapp_access.miniapp_open_for(PLAIN) is False
        assert await miniapp_access.miniapp_open_for(None) is False
        await db.set_setting("miniapp_enabled", "on")
        assert await miniapp_access.miniapp_open_for(PLAIN) is True

    asyncio.run(go())


def test_is_app_staff_drops_bot_only_caps():
    assert miniapp_access.is_app_staff({"source_links"}) is False
    assert miniapp_access.is_app_staff({"source_links", "checkin"}) is True
    assert miniapp_access.is_app_staff(set()) is False


class _User:
    def __init__(self, uid):
        self.id = uid


class _Chat:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid):
        self.text = "📱 Приложение"
        self.from_user = _User(uid)
        self.chat = _Chat(uid)
        self.calls = []

    async def answer(self, text=None, **kwargs):
        self.calls.append((text, kwargs))


def test_menu_button_handler_opens_app_for_staff_when_off(bot_env):
    msg = _Msg(ADMIN)
    asyncio.run(ua_mod.open_miniapp_button(msg))
    btn = msg.calls[0][1]["reply_markup"].inline_keyboard[0][0]
    assert isinstance(btn.web_app, WebAppInfo)
    assert btn.web_app.url == "https://yl.example.test/app"


def test_menu_button_handler_shows_disabled_text_to_delegate_when_off(bot_env):
    msg = _Msg(PLAIN)
    asyncio.run(ua_mod.open_miniapp_button(msg))
    text, kwargs = msg.calls[0]
    assert text == "Приложение временно недоступно. Всё то же самое есть в боте."
    assert "reply_markup" not in kwargs


def _menu_texts(uid):
    kb = asyncio.run(builders.get_main_menu_kb(uid))
    return [b.text for row in kb.keyboard for b in row]


def test_main_menu_shows_app_button_to_staff_only_when_off(bot_env):
    asyncio.run(db.set_setting("menu_miniapp", "on"))
    assert "📱 Приложение" in _menu_texts(ADMIN)
    assert "📱 Приложение" not in _menu_texts(PLAIN)


def test_settings_screen_explains_toggle_in_human_words(bot_env):
    text = asyncio.run(admin_miniapp.render_miniapp_settings_text())
    assert "делегаты не видят приложение" in text
    assert "менеджеры по-прежнему могут им пользоваться" in text


def test_browser_app_button_present_when_off(bot_env):
    import handlers.admin as admin_mod

    kb = asyncio.run(admin_mod._stats_keyboard_for(ADMIN))
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert "https://yl.example.test/app" in urls


# ── Ревью: выключенная оболочка без личности ничего не рассказывает о событии ─────────────

def test_off_shell_hides_event_details_from_anonymous(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_staff_off_shell.db")
    _standard_seed()  # event_name = «форума YouLead», разделы включены
    _set("miniapp_logo", "AgACAgIAAxkBAAIBlogo")
    client = _client(_cfg(db_path))
    on = client.get("/app").text
    assert "форума YouLead" in on and "AgACAgIAAxkBAAIBlogo" in on
    _set("miniapp_enabled", "off")
    off = client.get("/app")
    assert off.status_code == 200
    assert "форума YouLead" not in off.text
    assert "AgACAgIAAxkBAAIBlogo" not in off.text
    assert 'data-sections=""' in off.text
