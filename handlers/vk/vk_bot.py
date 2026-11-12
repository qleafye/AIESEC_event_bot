"""Слушатель сообщества ВК. Сеть не держит цикл aiogram."""
from __future__ import annotations

import asyncio
import logging

from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll

from keyboards.vk_builders import vk_main_menu_keyboard
from services.infra.background import spawn
from services.vk.vk_api_client import send_vk_message, vk_session

logger = logging.getLogger(__name__)

_RETRY_SECONDS = 30


_LINK_PREFIX = "link_"
_LINK_DEAD = "Ссылка недействительна или истекла. Попросите новую командой /link_vk в Telegram."
_LINK_NO_CARD = (
    "Карточка в Telegram не найдена. Пройдите регистрацию там и запросите новую ссылку командой /link_vk."
)
_LINK_TAKEN = "Этот Telegram уже связан с ВК. Вторая анкета не начинается."
_LINK_VK_EXISTS = "В этом ВК уже есть анкета. Вторая не начинается, и карточки сами не склеиваются."
_LINK_FAILED = "Связать аккаунты не получилось. Запросите новую ссылку командой /link_vk в Telegram."
_LINK_DONE = "Аккаунты связаны. Анкета уже есть, вторая не начинается."
_LINK_OPEN = "Аккаунты связаны. Допишите анкету там, где начали. Вторая не начинается."
_WRITE_HERE = "пиши сюда"
_WRITE_HERE_DONE = "Теперь письма будут приходить сюда."
_WRITE_HERE_UNLINKED = (
    "Аккаунты ещё не связаны. Откройте ссылку из Telegram командой /link_vk."
)


def _link_secret(ref: str | None) -> str | None:
    text = (ref or "").strip()
    if not text.startswith(_LINK_PREFIX):
        return None
    secret = text[len(_LINK_PREFIX):].strip()
    return secret or None


async def _handle_link_secret(vk_id: int, secret: str) -> None:
    """Секрет пишет номер ВК на карточку Telegram. Чужую карточку ВК не трогает."""
    from database.db import consume_link_secret, get_user, get_vk_user, link_vk_account

    telegram_id = await consume_link_secret(secret)
    if not telegram_id:
        await send_vk_message(vk_id, _LINK_DEAD)
        return
    tg_user = await get_user(telegram_id)
    if tg_user is None:
        await send_vk_message(vk_id, _LINK_NO_CARD)
        return
    if tg_user.get("vk_id"):
        await send_vk_message(vk_id, _LINK_TAKEN)
        return
    vk_user = await get_vk_user(vk_id)
    if vk_user is not None:
        await send_vk_message(vk_id, _LINK_VK_EXISTS)
        return
    before = dict(tg_user)
    try:
        await link_vk_account(telegram_id, vk_id)
    except ValueError:
        await send_vk_message(vk_id, _LINK_FAILED)
        return
    linked = await get_user(telegram_id)
    if linked is None or linked.get("vk_id") != int(vk_id):
        await send_vk_message(vk_id, _LINK_FAILED)
        return
    # Склейка — только номер ВК. Остальные поля, включая будущую ленту, не двигаются.
    rest = dict(linked)
    rest["vk_id"] = before.get("vk_id")
    if rest != before:
        logger.error("VK link changed extra fields for telegram %s", telegram_id)
    if before.get("status") in ("approved", "pending"):
        await send_vk_message(vk_id, _LINK_DONE)
        return
    await send_vk_message(vk_id, _LINK_OPEN)


async def _switch_feed_to_vk(vk_id: int) -> None:
    """«Пиши сюда» переносит ленту на карточку Telegram, с которой связан этот ВК.
    Без склейки колонка не меняется и вторая анкета не начинается."""
    from database.db import get_vk_user, set_active_messenger

    user = await get_vk_user(vk_id)
    telegram_id = user.get("telegram_id") if user else None
    if not telegram_id:
        await send_vk_message(vk_id, _WRITE_HERE_UNLINKED)
        return
    before = dict(user)
    if not await set_active_messenger(int(telegram_id), "vk"):
        await send_vk_message(vk_id, _WRITE_HERE_UNLINKED)
        return
    after = await get_vk_user(vk_id)
    if after is None:
        await send_vk_message(vk_id, _WRITE_HERE_UNLINKED)
        return
    rest = dict(after)
    rest["active_messenger"] = before.get("active_messenger")
    if rest != before:
        logger.error("VK feed switch changed extra fields for telegram %s", telegram_id)
    await send_vk_message(vk_id, _WRITE_HERE_DONE)


async def handle_vk_message(
    vk_id: int, text: str, payload: dict | None = None, ref: str | None = None,
) -> None:
    """Личное входящее. Ссылка склейки важнее анкеты, текст ссылки в лог не пишется."""
    del payload
    logger.info("VK message from %s", vk_id)
    secret = _link_secret(ref)
    if secret:
        await _handle_link_secret(vk_id, secret)
        return
    from handlers.vk.vk_registration import process_vk_registration_step, start_vk_registration

    body = (text or "").strip()
    if body.casefold() == _WRITE_HERE:
        await _switch_feed_to_vk(vk_id)
        return
    if body == "/start" or body.startswith("/start "):
        await start_vk_registration(vk_id)
        return
    await process_vk_registration_step(vk_id, body)


def _incoming_user(event) -> tuple[int, str, str | None] | None:
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
    if not isinstance(text, str):
        text = str(text)
    ref = message.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        ref = None
    else:
        ref = ref.strip()
    return user_id, text, ref


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


async def _safe_handle(vk_id: int, text: str, ref: str | None = None) -> None:
    try:
        await handle_vk_message(vk_id, text, ref=ref)
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
            spawn(_safe_handle(incoming[0], incoming[1], incoming[2]))
            scheduled += 1
    return scheduled


def start_vk_listener() -> None:
    """Фоновая задача до поллинга Telegram."""
    spawn(run_vk_listener())
