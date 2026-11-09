"""Сброс города: callback_data не длиннее 64 байт, токен ключа стабилен.

Токен — не номер строки реестра: добавление ключа не должно сдвигать чужие кнопки.
Неизвестный токен не удаляет городское значение.
"""
import asyncio
from pathlib import Path

from config import config
from database import db
from domain.cities import parse_cities, per_city_key
from domain.settings.callback_token import (
    CB_LIMIT,
    TOKEN_HEX,
    city_reset_callback,
    resolve_setting_token,
    setting_token,
)
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.settings import admin_settings
from tests.test_admin_percity_ui import ADMIN_ID, FakeCallback, FakeState, _admin_ready


def _project_city_codes() -> list[str]:
    """Список городов проекта из EVENT_CITIES, не кэш, который другой тест мог подменить."""
    codes = [row["code"] for row in parse_cities(config.EVENT_CITIES)]
    assert codes, "в EVENT_CITIES нет ни одного города — сторож проверил бы пустоту"
    return codes


def _unused_token() -> str:
    for ch in "0123456789abcdef":
        token = "." + ch * TOKEN_HEX
        if resolve_setting_token(token) is None:
            return token
    raise AssertionError("не нашли токен, которого нет в реестре")


def test_setting_tokens_are_unique_and_roundtrip():
    seen: dict[str, str] = {}
    for key in SETTINGS_SCHEMA:
        token = setting_token(key)
        assert token not in seen, (seen[token], key, token)
        seen[token] = key
        assert resolve_setting_token(token) == key
        assert token not in SETTINGS_SCHEMA
        assert not key.startswith(".")


def test_per_city_reset_callbacks_fit_in_64_bytes():
    codes = _project_city_codes()
    per_city = [key for key, meta in SETTINGS_SCHEMA.items() if meta.get("per_city")]
    assert per_city
    for key in per_city:
        ask = city_reset_callback("ask", key)
        back = city_reset_callback("back", key)
        assert ask.startswith("settings_reset_city:") and not ask.startswith("settings_reset_city_go:")
        assert len(ask.encode("utf-8")) <= CB_LIMIT, (key, ask)
        assert len(back.encode("utf-8")) <= CB_LIMIT, (key, back)
        assert resolve_setting_token(ask.split(":", 1)[1]) == key
        raw_back = f"settings_edit:{key}"
        if len(raw_back.encode("utf-8")) <= CB_LIMIT:
            assert back == raw_back
        else:
            assert back == f"settings_edit:{setting_token(key)}"
            assert resolve_setting_token(back.split(":", 1)[1]) == key
        for code in codes:
            go = city_reset_callback("go", key, code)
            assert len(go.encode("utf-8")) <= CB_LIMIT, (key, code, len(go.encode("utf-8")))
            payload, got = go.split(":", 1)[1].rsplit(":", 1)
            assert got == code
            assert resolve_setting_token(payload) == key


def test_back_callback_uses_token_only_when_raw_key_does_not_fit():
    assert city_reset_callback("back", "start_text") == "settings_edit:start_text"
    long = "k" * (CB_LIMIT - len("settings_edit:") + 1)
    back = city_reset_callback("back", long)
    assert back == "settings_edit:" + setting_token(long)
    assert len(back.encode("utf-8")) <= CB_LIMIT
    assert len(f"settings_edit:{long}".encode("utf-8")) > CB_LIMIT


def test_reset_buttons_share_one_builder():
    for rel in ("handlers/settings/admin_settings.py", "handlers/forum/admin_forum_date.py"):
        text = Path(rel).read_text(encoding="utf-8")
        assert 'f"settings_reset_city' not in text, rel
        assert "f'settings_reset_city" not in text, rel
        assert "city_reset_callback(" in text, rel


def test_settings_edit_resolves_token_to_the_same_key(tmp_path):
    _admin_ready(tmp_path, db_name="reset_cb_edit.db")
    state = FakeState()
    cb = FakeCallback("settings_edit:" + setting_token("start_text"))
    asyncio.run(admin_settings.settings_edit_start(cb, state))
    assert state.data.get("setting_key") == "start_text"


def test_unknown_reset_token_does_not_delete(tmp_path, monkeypatch):
    _admin_ready(tmp_path, db_name="reset_cb_unknown.db")
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    from domain.cities import set_admin_city
    assert asyncio.run(set_admin_city(ADMIN_ID, "spb"))
    composed = per_city_key("start_text", "spb")
    asyncio.run(db.set_setting(composed, "Питер"))

    deleted: list[str] = []
    real = admin_settings.delete_setting_by_admin

    async def spy(admin_id, key):
        deleted.append(key)
        await real(admin_id, key)

    monkeypatch.setattr(admin_settings, "delete_setting_by_admin", spy)
    token = _unused_token()
    go = FakeCallback(f"settings_reset_city_go:{token}:spb")
    asyncio.run(admin_settings.settings_reset_city_go(go))
    ask = FakeCallback(f"settings_reset_city:{token}")
    asyncio.run(admin_settings.settings_reset_city(ask))
    assert deleted == []
    assert asyncio.run(db.get_setting(composed)) == "Питер"
    assert go.answers and go.answers[0][0] == "Неизвестная кнопка"
    assert ask.answers and ask.answers[0][0] == "Неизвестная кнопка"

    edit = FakeCallback(f"settings_edit:{token}")
    asyncio.run(admin_settings.settings_edit_start(edit, FakeState()))
    assert edit.answers and edit.answers[0][0] == "Неизвестная кнопка"
    assert deleted == []
    assert asyncio.run(db.get_setting(composed)) == "Питер"
