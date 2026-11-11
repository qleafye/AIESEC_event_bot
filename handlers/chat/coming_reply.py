"""Кнопки «Дойду» и «Не дойду» у одобренного.

Час приезда не спрашиваем. Из чата этим ответом никого не удаляем.
"""
from __future__ import annotations

from aiogram import F, types
from aiogram.filters import StateFilter

from database.db import COMING_NO, COMING_YES, set_coming_reply
from handlers.user_actions import router

BUTTON_YES = "Дойду"
BUTTON_NO = "Не дойду"
_NOT_FOR_THEM = (
    "Кнопки «Дойду» и «Не дойду» — для одобренных участников. "
    "Дождитесь решения по заявке."
)
_SAVED = {
    COMING_YES: "Записала: дойду.",
    COMING_NO: "Записала: не дойду.",
}


async def _store(message: types.Message, reply: str) -> None:
    result = await set_coming_reply(message.from_user.id, reply)
    if result != "saved":
        await message.answer(_NOT_FOR_THEM)
        return
    await message.answer(_SAVED[reply])


@router.message(StateFilter(None), F.text == BUTTON_YES)
async def coming_yes(message: types.Message) -> None:
    await _store(message, COMING_YES)


@router.message(StateFilter(None), F.text == BUTTON_NO)
async def coming_no(message: types.Message) -> None:
    await _store(message, COMING_NO)
