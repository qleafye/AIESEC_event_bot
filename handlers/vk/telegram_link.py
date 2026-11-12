"""Команда /link_vk в Telegram: одноразовая ссылка в сообщество.

Секрет приносит сам ВК, когда человек открывает ссылку. Ник анкеты сюда не входит.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.db import create_link_secret, get_user
from domain.settings.schema import get_setting_typed

router = Router()

_NO_CARD = "Сначала пройдите регистрацию в этом боте. После этого /link_vk даст ссылку."
_ALREADY = "ВК уже связан. Вторая анкета не начинается."
_NOT_READY = "Сообщество ВК ещё не включено. Попросите менеджера включить его и снова напишите /link_vk."
_GROUP = "Напишите /link_vk мне в личные сообщения. Ссылку в общий чат не пришлю."


def community_club_id(raw) -> str | None:
    """Номер сообщества без минуса. Нечисло — ссылку собрать нельзя."""
    text = str(raw or "").strip()
    if text.startswith("-"):
        text = text[1:]
    if not text.isdigit() or int(text) == 0:
        return None
    return text


def vk_link(club_id: str, secret: str) -> str:
    return f"https://vk.me/club{club_id}?ref=link_{secret}"


@router.message(Command("link_vk"))
async def link_vk_command(message: Message) -> None:
    """Ссылка на 15 минут. Повторно уже связанный аккаунт её не получает."""
    chat = getattr(message, "chat", None)
    if chat is not None and getattr(chat, "type", "private") != "private":
        await message.answer(_GROUP)
        return
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.answer(_NO_CARD)
        return
    if user.get("vk_id"):
        await message.answer(_ALREADY)
        return
    enabled = await get_setting_typed("vk_enabled")
    club_id = community_club_id(await get_setting_typed("vk_community_id"))
    if enabled != "on" or club_id is None:
        await message.answer(_NOT_READY)
        return
    secret = await create_link_secret(user_id)
    await message.answer(
        "Откройте ссылку в ВК в течение 15 минут:\n"
        f"{vk_link(club_id, secret)}\n\n"
        "Её должен открыть тот аккаунт ВК, который нужно связать. "
        "Если анкета уже сдана, вторая не начнётся."
    )
