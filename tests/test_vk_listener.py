"""Слушатель сообщества: выключенный не открывает Long Poll, чужие события молчат."""
import asyncio
import logging
from types import SimpleNamespace

from vk_api.bot_longpoll import VkBotEventType

import handlers.vk.vk_bot as vk_bot
import services.vk.vk_api_client as vk_client
from handlers.vk.vk_bot import handle_vk_message, run_vk_listener, start_vk_listener

SECRET = "vk1.a.secret-token-value"


def _settings(mapping):
    async def _get(key):
        return mapping[key]

    return _get


def _forbid_network(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("long poll")

    monkeypatch.setattr(vk_bot, "VkBotLongPoll", _boom)
    monkeypatch.setattr(vk_client.vk_api, "VkApi", _boom)


def test_listener_disabled_when_off(monkeypatch, caplog):
    monkeypatch.setattr(
        "domain.settings.schema.get_setting_typed",
        _settings({
            "vk_enabled": "off",
            "vk_community_token": SECRET,
            "vk_community_id": "555",
        }),
    )
    _forbid_network(monkeypatch)
    with caplog.at_level(logging.INFO):
        scheduled = asyncio.run(run_vk_listener(max_checks=1))
    assert scheduled == 0
    assert SECRET not in caplog.text


def test_listener_disabled_without_key_or_number(monkeypatch):
    cases = (
        {"vk_enabled": "on", "vk_community_token": "", "vk_community_id": "555"},
        {"vk_enabled": "on", "vk_community_token": SECRET, "vk_community_id": "клуб"},
        {"vk_enabled": "on", "vk_community_token": "  ", "vk_community_id": "-555"},
    )
    for mapping in cases:
        monkeypatch.setattr("domain.settings.schema.get_setting_typed", _settings(mapping))
        _forbid_network(monkeypatch)
        assert asyncio.run(run_vk_listener(max_checks=1)) == 0


def test_listener_starts_when_enabled(monkeypatch, caplog):
    monkeypatch.setattr(
        "domain.settings.schema.get_setting_typed",
        _settings({
            "vk_enabled": "on",
            "vk_community_token": SECRET,
            "vk_community_id": "-555",
        }),
    )
    seen = {}

    class _Session:
        def __init__(self, token, config=None, **_kwargs):
            self.token = token
            self.config = config

    class _LongPoll:
        def __init__(self, vk, group_id, wait=25):
            seen["token"] = vk.token
            seen["group_id"] = group_id
            seen["config"] = vk.config.__name__

        def check(self):
            return [
                SimpleNamespace(
                    type=VkBotEventType.MESSAGE_NEW,
                    from_user=True,
                    message={"from_id": 42, "text": "привет", "out": 0},
                ),
                SimpleNamespace(
                    type=VkBotEventType.MESSAGE_NEW,
                    from_user=True,
                    message={"from_id": 42, "text": "эхо", "out": 1},
                ),
                SimpleNamespace(
                    type=VkBotEventType.MESSAGE_NEW,
                    from_user=False,
                    message={"from_id": 42, "text": "чат", "out": 0},
                ),
                SimpleNamespace(
                    type=VkBotEventType.MESSAGE_REPLY,
                    from_user=True,
                    message={"from_id": 7, "text": "ответ", "out": 0},
                ),
            ]

    monkeypatch.setattr(vk_client.vk_api, "VkApi", _Session)
    monkeypatch.setattr(vk_bot, "VkBotLongPoll", _LongPoll)
    handled = []

    async def _handle(vk_id, text, payload=None):
        handled.append((vk_id, text))

    monkeypatch.setattr(vk_bot, "handle_vk_message", _handle)

    async def _run():
        scheduled = await run_vk_listener(max_checks=1)
        from services.infra.background import _background_tasks
        await asyncio.gather(
            *(task for task in list(_background_tasks) if not task.done()),
        )
        return scheduled

    with caplog.at_level(logging.INFO):
        scheduled = asyncio.run(_run())
    assert scheduled == 1
    assert handled == [(42, "привет")]
    assert seen == {"token": SECRET, "group_id": 555, "config": "MemoryConfig"}
    assert SECRET not in caplog.text
    assert "привет" not in caplog.text


def test_handle_message(monkeypatch, caplog):
    sent = []

    async def _keyboard(vk_id, event_city=None):
        assert vk_id == 7
        return '{"one_time": false, "buttons": []}'

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text, keyboard_json))

    monkeypatch.setattr(vk_bot, "vk_main_menu_keyboard", _keyboard)
    monkeypatch.setattr(vk_bot, "send_vk_message", _send)
    with caplog.at_level(logging.INFO):
        asyncio.run(handle_vk_message(7, "секретное-слово"))
    assert sent == [(7, "Привет!", '{"one_time": false, "buttons": []}')]
    assert "секретное-слово" not in caplog.text


def test_start_spawns_listener(monkeypatch):
    spawned = []

    def _spawn(coro):
        spawned.append(coro.cr_code.co_name)
        coro.close()

    monkeypatch.setattr(vk_bot, "spawn", _spawn)
    start_vk_listener()
    assert spawned == ["run_vk_listener"]
