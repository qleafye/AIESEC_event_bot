"""Экраны доступа к Яндексу: ключи приложения и вход по коду подтверждения. Харнесс —
tests/test_roles_phase8.py (настоящий Router.propagate_event)."""
import asyncio
import logging

import pytest

from config import config
from aiogram.dispatcher.event.bases import UNHANDLED

from database import ext_forms_db as xdb
from services import ext_forms_yandex as yx
from tests.test_roles_phase8 import (
    ADMIN_ID, FakeBot, FakeMessage, _fresh_state, _roles_ready, dispatch_callback, dispatch_message,
)

CID = "0123456789abcdef0123456789abcdef"
SECRET = "fedcba9876543210fedcba9876543210"


class DelBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


@pytest.fixture(autouse=True)
def _message_id(monkeypatch):
    # у настоящего Message есть message_id; у фейка харнесса нет
    monkeypatch.setattr(FakeMessage, "message_id", 42, raising=False)


def _run(coro):
    return asyncio.run(coro)


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _all_text(ev):
    return " ".join([ev.text or ""] + [a[0] for a in ev.answers])


def _no_env_keys(monkeypatch):
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_SECRET", None, raising=False)


def _msg(text, state, bot=None, st="ExtFormAppKeys:client_id"):
    return dispatch_message(text, ADMIN_ID, raw_state=f"{st}", state=state, bot=bot or DelBot())


def test_appkeys_screen_none_and_instruction(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _no_env_keys(monkeypatch)
    res, ev = dispatch_callback("extf_appkeys", ADMIN_ID)
    assert res is not UNHANDLED
    assert "Ключи не заданы" in ev.message.text
    assert "verification_code" in ev.message.text
    assert "аккаунт Яндекса под бота" in ev.message.text
    assert "extf_appkeys_edit" in _datas(ev.message.markup)


def test_appkeys_screen_in_bot_and_env(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _no_env_keys(monkeypatch)
    _run(xdb.set_app_secret("yandex_client_id", CID, 1))
    _run(xdb.set_app_secret("yandex_client_secret", SECRET, 1))
    _, ev = dispatch_callback("extf_appkeys", ADMIN_ID)
    assert "Ключи заданы в боте" in ev.message.text
    assert SECRET not in ev.message.text


def test_appkeys_screen_env_only(tmp_path, monkeypatch):
    from pydantic import SecretStr
    _roles_ready(tmp_path)
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_ID", CID, raising=False)
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_SECRET", SecretStr(SECRET), raising=False)
    _, ev = dispatch_callback("extf_appkeys", ADMIN_ID)
    assert "файла настроек сервера" in ev.message.text
    assert SECRET not in ev.message.text


def test_appkeys_wizard_saves_and_deletes_secret(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _no_env_keys(monkeypatch)
    state = _fresh_state(ADMIN_ID)
    res, ev = dispatch_callback("extf_appkeys_edit", ADMIN_ID, state=state)
    assert res is not UNHANDLED
    assert _run(state.get_state()) == "ExtFormAppKeys:client_id"

    _, ev = _msg("не id", state)
    assert "Не похоже на ClientID" in _all_text(ev)

    _, ev = _msg(CID.upper(), state)
    assert _run(state.get_state()) == "ExtFormAppKeys:client_secret"

    bot = DelBot()
    ev_msg = _msg(SECRET, state, bot=bot, st="ExtFormAppKeys:client_secret")[1]
    assert bot.deleted, "сообщение с секретом должно быть удалено"
    assert "Ключи сохранены" in _all_text(ev_msg)
    assert all(SECRET not in a[0] for a in ev_msg.answers)
    assert _run(xdb.get_app_secret("yandex_client_id")) == CID
    assert _run(xdb.get_app_secret("yandex_client_secret")) == SECRET
    assert _run(state.get_state()) is None

    # секрет не должен попасть в bot_settings
    import sqlite3
    c = sqlite3.connect(config.DB_PATH)
    try:
        rows = c.execute("SELECT * FROM bot_settings").fetchall()
    finally:
        c.close()
    assert SECRET not in repr(rows)


def test_appkeys_cancel(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    dispatch_callback("extf_appkeys_edit", ADMIN_ID, state=state)
    _, ev = _msg("Отмена", state)
    assert "Ключи не менялись" in _all_text(ev)
    assert _run(state.get_state()) is None


def test_oauth_without_keys_points_to_appkeys(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _no_env_keys(monkeypatch)
    _, ev = dispatch_callback("extf_oauth", ADMIN_ID)
    assert "Сначала задайте ключи" in ev.message.text
    assert "extf_appkeys" in _datas(ev.message.markup)


def _keys(monkeypatch):
    _run(xdb.set_app_secret("yandex_client_id", CID, 1))
    _run(xdb.set_app_secret("yandex_client_secret", SECRET, 1))


def test_oauth_start_shows_steps_and_url_button(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _no_env_keys(monkeypatch)
    _keys(monkeypatch)
    state = _fresh_state(ADMIN_ID)
    _, ev = dispatch_callback("extf_oauth", ADMIN_ID, state=state)
    assert "Разрешить" in ev.message.text
    urls = [b.url for row in ev.message.markup.inline_keyboard for b in row if b.url]
    assert urls and CID in urls[0]
    assert "Открыть Яндекс" in _texts(ev.message.markup)
    assert _run(state.get_state()) == "ExtFormOAuth:code"


def test_oauth_bad_code_format(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.set_state("ExtFormOAuth:code"))
    _, ev = _msg("abc", state, st="ExtFormOAuth:code")
    assert "пришлите только цифры кода" in _all_text(ev)
    assert _run(state.get_state()) == "ExtFormOAuth:code"


def test_oauth_bad_code_rejected_resets_state(tmp_path, monkeypatch):
    _roles_ready(tmp_path)

    async def boom(code):
        raise yx.YandexApiError("bad_code", 400)
    monkeypatch.setattr(yx, "exchange_code", boom)
    state = _fresh_state(ADMIN_ID)
    _run(state.set_state("ExtFormOAuth:code"))
    _, ev = _msg("1234567", state, st="ExtFormOAuth:code")
    assert "Код не подошёл или устарел" in _all_text(ev)
    assert _run(state.get_state()) is None


def test_oauth_success_then_org_and_relogin_keeps_row(tmp_path, monkeypatch, caplog):
    _roles_ready(tmp_path)
    calls = []

    async def fake_exchange(code):
        calls.append(code)
        return {"access_token": "AT-secret-1", "refresh_token": "RT-secret-1",
                "expires_at": "2027-01-01 00:00:00"}
    monkeypatch.setattr(yx, "exchange_code", fake_exchange)
    caplog.set_level(logging.DEBUG)
    state = _fresh_state(ADMIN_ID)
    _run(state.set_state("ExtFormOAuth:code"))
    bot = DelBot()
    _, ev = _msg("12 34 567", state, bot=bot, st="ExtFormOAuth:code")
    assert calls == ["1234567"]
    assert bot.deleted
    assert "Администрировании" in _all_text(ev)
    assert all("AT-secret-1" not in a[0] for a in ev.answers)
    assert _run(state.get_state()) == "ExtFormOAuth:org_id"
    first = _run(xdb.get_yandex_connection())
    assert first["access_token"] == "AT-secret-1" and first["org_id"] is None

    _, ev = _msg("не число", state, st="ExtFormOAuth:org_id")
    assert "ID организации числом" in _all_text(ev)

    _, ev = _msg("7654321", state, st="ExtFormOAuth:org_id")
    assert "✅ Доступ к Яндекс Формам подключён" in _all_text(ev)
    conn = _run(xdb.get_yandex_connection())
    assert str(conn["org_id"]) == "7654321" and conn["id"] == first["id"]

    # повторный вход: та же строка
    _run(state.set_state("ExtFormOAuth:code"))
    _msg("7654321", state, st="ExtFormOAuth:code")
    again = _run(xdb.get_yandex_connection())
    assert again["id"] == first["id"]
    own = " ".join(r.getMessage() for r in caplog.records if not r.name.startswith("aiosqlite"))
    assert "AT-secret-1" not in own and "RT-secret-1" not in own


def test_oauth_noorg_button(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _run(xdb.upsert_yandex_connection(org_id=5, org_header="X-Org-Id", access_token="AT",
                                       refresh_token="RT", expires_at=None, by=1))
    state = _fresh_state(ADMIN_ID)
    _run(state.set_state("ExtFormOAuth:org_id"))
    res, ev = dispatch_callback("extf_oauth_noorg", ADMIN_ID, state=state)
    assert res is not UNHANDLED
    assert "✅ Доступ к Яндекс Формам подключён" in _all_text(ev.message)
    assert _run(xdb.get_yandex_connection())["org_id"] is None


def test_relogin_keeps_org_id_and_skips_org_step(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    _run(xdb.upsert_yandex_connection(org_id=777, org_header="X-Org-Id", access_token="OLD",
                                       refresh_token="RT", expires_at=None, by=1))

    async def fake_exchange(code):
        return {"access_token": "NEW", "refresh_token": "RT2", "expires_at": "2027-01-01 00:00:00"}
    monkeypatch.setattr(yx, "exchange_code", fake_exchange)
    state = _fresh_state(ADMIN_ID)
    _run(state.set_state("ExtFormOAuth:code"))
    _, ev = _msg("1234567", state, st="ExtFormOAuth:code")
    conn = _run(xdb.get_yandex_connection())
    assert conn["access_token"] == "NEW" and str(conn["org_id"]) == "777"
    assert _run(state.get_state()) is None
    assert "Организация осталась прежней" in _all_text(ev)


def test_cancel_at_org_step_is_honest(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.set_state("ExtFormOAuth:org_id"))
    _, ev = _msg("Отмена", state, st="ExtFormOAuth:org_id")
    text = _all_text(ev)
    assert "Вход выполнен без организации" in text and "не менялось" not in text
