"""Заявка на вход в чат города. Чужого бот не принимает и не отклоняет."""
from __future__ import annotations

import logging

from aiogram import Bot, F

from handlers.chat.group_chat import router
from services.chat_tracking import bound_chats
from services.city_chat_gate import may_approve

logger = logging.getLogger(__name__)

router.chat_join_request.filter(F.chat.type.in_({"group", "supergroup"}))


def _city_code(entries, chat_id) -> str:
    for entry in entries:
        if entry["chat_id"] == chat_id:
            return entry["city"] or ""
    return ""


@router.chat_join_request()
async def on_chat_join_request(event, bot: Bot) -> None:
    chat_id = event.chat.id
    user_id = event.from_user.id
    if not await may_approve(chat_id, user_id):
        return
    code = _city_code(await bound_chats(), chat_id)
    try:
        await bot.approve_chat_join_request(chat_id, user_id)
    except Exception as exc:
        logger.warning(
            "city_chat_join: заявку не принял, город %s: %s",
            code, type(exc).__name__,
        )
