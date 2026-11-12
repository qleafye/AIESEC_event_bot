"""Рассылка ВК: пауза между письмами, один сбой не останавливает остальных, картинка не уходит."""
import asyncio
import time

from services import vk_broadcast


def test_rate_limit(monkeypatch):
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text, keyboard_json))
        return True

    monkeypatch.setattr(vk_broadcast, "send_vk_message", _send)
    started = time.monotonic()
    result = asyncio.run(vk_broadcast.broadcast_vk("привет", None, [7, 8]))
    elapsed = time.monotonic() - started
    assert result == (2, 0)
    assert sent == [(7, "привет", None), (8, "привет", None)]
    assert elapsed >= 0.1


def test_fail_soft(monkeypatch):
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append(vk_id)
        if vk_id == 7:
            raise RuntimeError("сеть")
        return vk_id != 9

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(vk_broadcast, "send_vk_message", _send)
    monkeypatch.setattr(vk_broadcast.asyncio, "sleep", _no_wait)
    result = asyncio.run(vk_broadcast.broadcast_vk("привет", None, [7, 8, 9]))
    assert result == (1, 2)
    assert sent == [7, 8, 9]


def test_photo_is_not_sent(monkeypatch):
    async def _send(*_args, **_kwargs):
        raise AssertionError("картинка")

    monkeypatch.setattr(vk_broadcast, "send_vk_message", _send)
    result = asyncio.run(vk_broadcast.broadcast_vk("привет", "telegram-file", [7]))
    assert result == (0, 1)
