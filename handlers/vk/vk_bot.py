"""Слушатель сообщества ВК. Сеть не держит цикл aiogram."""
from __future__ import annotations

import asyncio
import logging

from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll

from keyboards.vk_builders import vk_main_menu_keyboard
from services.infra.background import spawn
from services.vk.vk_api_client import send_vk_message, vk_session

logger = logging.getLogger(__name__)

_GREETING = "Привет!"
_RETRY_SECONDS = 30


async def handle_vk_message(vk_id: int, text: str, payload: dict | None = None) -> None:
    """Личное входящее. Ответ на пункт меню появится, когда пункт попадёт в клавиатуру."""
    del text, payload
    logger.info("VK message from %s", vk_id)
    keyboard = await vk_main_menu_keyboard(vk_id)
    await send_vk_message(vk_id, _GREETING, keyboard)


def _incoming_user(event) -> tuple[int, str] | None:
    if getattr(event, "type", None) != VkBotEventType.MESSAGE_NEW:
        return None
    if not getattr(event, "from_user", False):
        return None
    message = getattr(event, "message", None) or {}
    if message.get("out"):
        return None
    user_id = message.get("from_id")
    if not isinstance(user_id, int) or user_id <= 0:
        return None
    text = message.get("text") or ""
    return user_id, text if isinstance(text, str) else str(text)


def _community_number(raw) -> int | None:
    text = str(raw or "").strip()
    if text.startswith("-"):
        text = text[1:]
    if not text.isdigit() or int(text) == 0:
        return None
    return int(text)


async def _listener_config() -> tuple[str, int] | None:
    from domain.settings.schema import get_setting_typed

    enabled = await get_setting_typed("vk_enabled")
    token = str(await get_setting_typed("vk_community_token") or "").strip()
    group_id = _community_number(await get_setting_typed("vk_community_id"))
    if enabled != "on" or not token or group_id is None:
        return None
    return token, group_id


def _open_longpoll(token: str, group_id: int) -> VkBotLongPoll:
    return VkBotLongPoll(vk_session(token), group_id)


async def _safe_handle(vk_id: int, text: str) -> None:
    try:
        await handle_vk_message(vk_id, text)
    except Exception as exc:
        logger.error("VK message handling failed for %s: %s", vk_id, type(exc).__name__)


async def run_vk_listener(max_checks: int | None = None) -> int:
    """Слушать сообщество. max_checks ограничивает тест; в боте его нет.

    listen() не отпускает поток. Один check() уходит в поток, событие
    обрабатывается уже в цикле aiogram. Выключенный слушатель засыпает и
    перечитывает настройки, чтобы включение обошлось без перезапуска.
    """
    scheduled = 0
    checks = 0
    longpoll = None
    opened_for: tuple[str, int] | None = None
    waiting_logged = False
    while max_checks is None or checks < max_checks:
        ready = await _listener_config()
        if ready is None:
            longpoll = None
            opened_for = None
            if not waiting_logged:
                logger.info(
                    "VK listener disabled (switch off, no key, or no community number)",
                )
                waiting_logged = True
            if max_checks is not None:
                return scheduled
            await asyncio.sleep(_RETRY_SECONDS)
            continue
        waiting_logged = False
        if longpoll is None or opened_for != ready:
            try:
                longpoll = await asyncio.to_thread(_open_longpoll, ready[0], ready[1])
                opened_for = ready
            except Exception as exc:
                logger.error(
                    "VK listener: не удалось включить прослушивание (%s)",
                    type(exc).__name__,
                )
                longpoll = None
                opened_for = None
                if max_checks is not None:
                    return scheduled
                await asyncio.sleep(_RETRY_SECONDS)
                continue
        try:
            events = await asyncio.to_thread(longpoll.check)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("VK listener: сбой чтения (%s)", type(exc).__name__)
            longpoll = None
            opened_for = None
            if max_checks is not None:
                return scheduled
            await asyncio.sleep(_RETRY_SECONDS)
            continue
        checks += 1
        for event in events or ():
            incoming = _incoming_user(event)
            if incoming is None:
                continue
            spawn(_safe_handle(incoming[0], incoming[1]))
            scheduled += 1
    return scheduled


def start_vk_listener() -> None:
    """Фоновая задача до поллинга Telegram."""
    spawn(run_vk_listener())
