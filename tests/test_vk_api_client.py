"""Отправка сообщения сообщества: ключ не в журнале, сбой VK не роняет вызов."""
import asyncio
import logging

import vk_api

from services.vk.vk_api_client import send_vk_message

SECRET = "vk1.a.secret-token-value"


class _Messages:
    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def send(self, **kwargs):
        if self._error is not None:
            raise self._error
        self.calls.append(kwargs)
        return 1


class _Api:
    def __init__(self, messages):
        self.messages = messages


class _Session:
    def __init__(self, token):
        self.token = token
        self._api = None

    def get_api(self):
        return self._api


def _patch(monkeypatch, messages, token=SECRET):
    session = _Session(token)
    session._api = _Api(messages)

    def _vk(token, config=None, **_kwargs):
        session.token = token
        session.config = config
        return session

    async def _setting(key):
        assert key == "vk_community_token"
        return token

    monkeypatch.setattr(vk_api, "VkApi", _vk)
    monkeypatch.setattr("domain.settings.schema.get_setting_typed", _setting)
    return session, messages


def test_send_message_success(monkeypatch):
    messages = _Messages()
    session, _ = _patch(monkeypatch, messages)
    asyncio.run(send_vk_message(42, "Привет"))
    assert session.token == SECRET
    assert session.config.__name__ == "MemoryConfig"
    assert messages.calls[0]["user_id"] == 42
    assert messages.calls[0]["message"] == "Привет"
    assert "keyboard" not in messages.calls[0]


def test_send_message_with_keyboard(monkeypatch):
    messages = _Messages()
    _patch(monkeypatch, messages)
    keyboard = '{"one_time": false, "buttons": []}'
    asyncio.run(send_vk_message(7, "Меню", keyboard))
    assert messages.calls[0]["keyboard"] == keyboard


def test_send_message_api_error(monkeypatch, caplog):
    error = vk_api.exceptions.ApiError(
        None, "messages.send", {"access_token": SECRET}, {},
        {"error_code": 901, "error_msg": "can't send"},
    )
    _patch(monkeypatch, _Messages(error=error))
    with caplog.at_level(logging.ERROR):
        asyncio.run(send_vk_message(7, "текст-секрета"))
    assert SECRET not in caplog.text
    assert "текст-секрета" not in caplog.text
    assert "901" in caplog.text


def test_send_message_unexpected_error(monkeypatch, caplog):
    _patch(monkeypatch, _Messages(error=RuntimeError(f"url?access_token={SECRET}")))
    with caplog.at_level(logging.ERROR):
        asyncio.run(send_vk_message(7, "текст-секрета"))
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text


def test_random_id_generated(monkeypatch):
    messages = _Messages()
    _patch(monkeypatch, messages)
    asyncio.run(send_vk_message(1, "a"))
    asyncio.run(send_vk_message(1, "b"))
    first, second = messages.calls[0]["random_id"], messages.calls[1]["random_id"]
    assert first != second
    assert 1 <= first < 2**31
    assert 1 <= second < 2**31


def test_send_skipped_without_token(monkeypatch, caplog):
    messages = _Messages()
    session, _ = _patch(monkeypatch, messages, token="")

    def _boom(token):
        raise AssertionError("VkApi")

    monkeypatch.setattr(vk_api, "VkApi", _boom)
    with caplog.at_level(logging.ERROR):
        asyncio.run(send_vk_message(7, "привет"))
    assert messages.calls == []
    assert session.token == ""
