"""Одно письмо — один мессенджер. Куда писать, читается с карточки в момент отправки."""
from __future__ import annotations

import logging

from database.db import get_user
from services.vk.vk_api_client import send_vk_message

logger = logging.getLogger(__name__)


async def send_to_delegate(
    user_id: int,
    text: str,
    bot=None,
    keyboard_json: str | None = None,
) -> None:
    """Письмо делегату с карточкой Telegram. user_id — telegram_id, не номер ВК.

    active_messenger vk — только ВК, и только если номер ВК на этой карточке есть.
    telegram и пусто — только Telegram. Во второй мессенджер то же письмо не уходит.
    Сбой ВК клиент пишет в журнал и глотает. Сбой Telegram поднимается вызывающему.
    Текст письма в журнал не попадает.
    """
    user = await get_user(user_id)
    if not user:
        logger.warning("Письмо делегату %s не ушло: карточки нет", user_id)
        return

    active = (user.get("active_messenger") or "").strip()

    if active == "vk":
        vk_id = user.get("vk_id")
        if not vk_id:
            logger.error("Письмо делегату %s не ушло: лента ВК, а номера ВК нет", user_id)
            return
        await send_vk_message(int(vk_id), text, keyboard_json)
        return

    if active in ("telegram", ""):
        telegram_id = user.get("telegram_id")
        if not telegram_id:
            logger.error("Письмо делегату %s не ушло: лента Telegram, а номера нет", user_id)
            return
        if bot is None:
            logger.error("Письмо делегату %s не ушло: для Telegram нужен бот", user_id)
            return
        await bot.send_message(telegram_id, text)
        return

    logger.warning("Письмо делегату %s не ушло: непонятно, куда писать", user_id)
