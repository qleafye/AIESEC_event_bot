"""Проверка ключа сообщества: право на сообщения или понятный отказ."""
import asyncio

import vk_api

from services.vk.vk_api_client import validate_vk_token


class _Groups:
    def __init__(self, mask=None, error=None):
        self._mask = mask
        self._error = error

    def getTokenPermissions(self):
        if self._error is not None:
            raise self._error
        return {"mask": self._mask}


class _Api:
    def __init__(self, groups):
        self.groups = groups


class _Session:
    def __init__(self, api):
        self._api = api

    def get_api(self):
        return self._api


def _patch(monkeypatch, groups):
    monkeypatch.setattr(
        vk_api,
        "VkApi",
        lambda token: _Session(_Api(groups)),
    )


def test_validate_token_success(monkeypatch):
    _patch(monkeypatch, _Groups(mask=4096))
    assert asyncio.run(validate_vk_token("secret")) == (True, None)


def test_validate_token_no_messages_scope(monkeypatch):
    _patch(monkeypatch, _Groups(mask=0))
    ok, text = asyncio.run(validate_vk_token("secret"))
    assert ok is False
    assert "не имеет права" in text
    assert "Сообщения сообщества" in text


def test_validate_token_api_error(monkeypatch):
    error = vk_api.exceptions.ApiError(
        None, "groups.getTokenPermissions", {}, {},
        {"error_code": 5, "error_msg": "User authorization failed"},
    )
    _patch(monkeypatch, _Groups(error=error))
    ok, text = asyncio.run(validate_vk_token("secret"))
    assert ok is False
    assert text.startswith("Ошибка VK API:")
