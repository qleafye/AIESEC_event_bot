"""Ключ сообщества ВК: кнопки, проверка до записи, секрет не возвращается."""
import asyncio
import logging

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from domain.settings.ops import commit_batch_item, needs_delegate_preview, validate_batch_item
from domain.settings.schema import SETTINGS_SCHEMA, client_omits_secret
from handlers.settings import admin_sections, admin_settings
from handlers.settings.admin_settings_enum import enum_rows
from miniapp.routers.settings import _CityCtx, _item_for
from services.settings.audit import set_setting_by_admin
from tests._dbtpl import fast_init_db

ADMIN_ID = 900001
SECRET = "vk1.a.secret-token-value"
OLD = "vk1.a.old-secret"
NO_RIGHT = (
    "Токен не имеет права на сообщения сообщества.\n\n"
    "поставьте галочку «Сообщения сообщества» и пришлите новый ключ."
)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_settings.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _state():
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, text, user_id=ADMIN_ID):
        self.text = text
        self.html_text = text
        self.from_user = _User(user_id)
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


def _patch(monkeypatch, result):
    async def fake(_token):
        return result

    monkeypatch.setattr("services.vk.vk_api_client.validate_vk_token", fake)


def test_vk_settings_in_schema():
    enabled = SETTINGS_SCHEMA["vk_enabled"]
    token = SETTINGS_SCHEMA["vk_community_token"]
    community = SETTINGS_SCHEMA["vk_community_id"]
    assert enabled["type"] == "enum" and enabled["options"] == ["on", "off"]
    assert enabled["default"] == "off" and not enabled.get("per_city")
    assert token["sensitive"] is True and token["group"] == "vk"
    assert community["group"] == "vk" and community["pile"] == "service"
    assert not token.get("per_city") and not community.get("per_city")
    labels = [label for label, _tok, _keys in admin_settings.SETTINGS_GROUPS]
    assert "💬 ВКонтакте" in labels
    assert ("group", "vk") in admin_sections._declared_rows("manage")
    labels = [btn.text for row in enum_rows("vk_enabled", None) for btn in row]
    assert labels == ["Включено", "Выключено"]
    assert not needs_delegate_preview("vk_community_token")
    assert not needs_delegate_preview("vk_community_id")


def test_sensitive_display_hides_token(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("vk_community_token", SECRET))
    text, _kb = asyncio.run(admin_settings._settings_edit_screen("vk_community_token", None))
    assert SECRET not in text
    assert "Установлен: да" in text

    item = asyncio.run(_item_for("vk_community_token", _CityCtx(False, None, [])))
    blob = f"{item['display']} {item['raw']} {item['value']} {item['help']}"
    assert SECRET not in blob
    assert item["display"] == "Установлен: да"
    assert item["raw"] is None and item["value"] == ""
    assert client_omits_secret("vk_community_token", None)
    assert client_omits_secret("vk_community_token", "чужое")
    assert not client_omits_secret("event_name", None)


def test_empty_secret_says_not_set(tmp_path):
    _ready(tmp_path)
    text, _kb = asyncio.run(admin_settings._settings_edit_screen("vk_community_token", None))
    assert "Не задан" in text


def test_save_invalid_token_drops_old_key(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.set_setting("vk_community_token", OLD))
    _patch(monkeypatch, (False, NO_RIGHT))
    state = _state()
    asyncio.run(state.update_data(setting_key="vk_community_token"))
    msg = _Msg("vk1.a.rejected")
    asyncio.run(admin_settings.settings_edit_value(msg, state))
    assert asyncio.run(db.get_setting("vk_community_token")) is None
    assert any("Сообщения сообщества" in text for text in msg.answers)
    assert all("vk1.a.rejected" not in text and OLD not in text for text in msg.answers)


def test_network_error_keeps_old_key(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.set_setting("vk_community_token", OLD))
    _patch(monkeypatch, (False, "Не удалось проверить токен: timeout"))
    state = _state()
    asyncio.run(state.update_data(setting_key="vk_community_token"))
    asyncio.run(admin_settings.settings_edit_value(_Msg("vk1.a.new"), state))
    assert asyncio.run(db.get_setting("vk_community_token")) == OLD


def test_save_valid_token(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch(monkeypatch, (True, None))
    state = _state()
    asyncio.run(state.update_data(setting_key="vk_community_token"))
    msg = _Msg(SECRET)
    asyncio.run(admin_settings.settings_edit_value(msg, state))
    assert asyncio.run(db.get_setting("vk_community_token")) == SECRET
    assert all(SECRET not in text for text in msg.answers)


def test_reset_does_not_call_vk(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.set_setting("vk_community_token", OLD))

    async def boom(_token):
        raise AssertionError("сброс не должен звать ВК")

    monkeypatch.setattr("services.vk.vk_api_client.validate_vk_token", boom)
    state = _state()
    asyncio.run(state.update_data(setting_key="vk_community_token"))
    asyncio.run(admin_settings.settings_edit_value(_Msg("-"), state))
    assert asyncio.run(db.get_setting("vk_community_token")) is None


def test_batch_rejects_token_without_messages_right(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.set_setting("vk_community_token", OLD))
    _patch(monkeypatch, (False, NO_RIGHT))

    async def go():
        return await validate_batch_item(
            "vk_community_token", "vk1.a.rejected",
            visible_codes=[], selected_city=None, cities_on=False,
        )

    check = asyncio.run(go())
    assert check.error and "Сообщения сообщества" in check.error
    assert check.value is None
    assert asyncio.run(db.get_setting("vk_community_token")) is None


def test_batch_keeps_key_when_vk_is_unreachable(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.set_setting("vk_community_token", OLD))
    _patch(monkeypatch, (False, "Ошибка VK API: timeout"))

    async def go():
        return await validate_batch_item(
            "vk_community_token", "vk1.a.new",
            visible_codes=[], selected_city=None, cities_on=False,
        )

    check = asyncio.run(go())
    assert check.error and "Ошибка VK API" in check.error
    assert asyncio.run(db.get_setting("vk_community_token")) == OLD


def test_batch_saves_valid_token_without_logging_it(tmp_path, monkeypatch, caplog):
    _ready(tmp_path)
    _patch(monkeypatch, (True, None))

    async def go():
        check = await validate_batch_item(
            "vk_community_token", SECRET,
            visible_codes=[], selected_city=None, cities_on=False,
        )
        assert check.error is None and check.value == SECRET
        await commit_batch_item("vk_community_token", check.value, ADMIN_ID)

    with caplog.at_level(logging.INFO):
        asyncio.run(go())
    assert asyncio.run(db.get_setting("vk_community_token")) == SECRET
    assert SECRET not in caplog.text
    assert "установлен" in caplog.text


def test_set_setting_log_masks_token(tmp_path, caplog):
    _ready(tmp_path)
    with caplog.at_level(logging.INFO):
        asyncio.run(set_setting_by_admin(ADMIN_ID, "vk_community_token", SECRET))
    assert SECRET not in caplog.text
    assert "установлен" in caplog.text
