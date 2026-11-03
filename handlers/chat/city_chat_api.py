"""Тонкая обёртка Bot API для закрытой ссылки чата города.

Хендлеров нет: заявки и исключение ходят в Bot сами, подстановку делает
`services.city_chat_link.render_for_delegate`.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class _InviteClient:
    def __init__(self, bot):
        self._bot = bot

    async def edit_invite(self, chat_id, invite_link, name, expire_date, member_limit):
        return await self._bot.edit_chat_invite_link(
            chat_id=chat_id,
            invite_link=invite_link,
            name=name,
            expire_date=expire_date,
            member_limit=member_limit,
        )

    async def create_invite(self, chat_id, name, expire_date, member_limit, creates_join_request):
        kwargs = {
            "chat_id": chat_id,
            "name": name,
            "creates_join_request": creates_join_request,
        }
        if expire_date is not None:
            kwargs["expire_date"] = expire_date
        if member_limit is not None:
            kwargs["member_limit"] = member_limit
        return await self._bot.create_chat_invite_link(**kwargs)

    async def revoke_invite(self, chat_id, invite_link):
        return await self._bot.revoke_chat_invite_link(
            chat_id=chat_id, invite_link=invite_link,
        )


def client_from_bot(bot) -> _InviteClient:
    return _InviteClient(bot)


async def notify_link_failure(bot, city, phrase: str) -> None:
    from services.city_chat_link import recipients_for_city

    for uid in await recipients_for_city(city):
        try:
            await bot.send_message(uid, phrase)
        except Exception as exc:
            logger.warning(
                "city_chat_api: notify failed city=%s exc=%s",
                city or "",
                type(exc).__name__,
            )
