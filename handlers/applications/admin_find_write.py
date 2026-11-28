"""«✉️ Написать» с карточки /find — одно сообщение менеджера уходит делегату.

Если делегат читает письма во ВК, уходит текст. Иначе сообщение копируется в Telegram как есть.
"""
from __future__ import annotations

import html as html_module
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from handlers.states import FindWrite
from services.applications.personal_letter import gate, mark_sent, person_name
from services.messenger_router import send_to_delegate

logger = logging.getLogger(__name__)


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _cancel_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"findw_cancel:{tid}")],
    ])


def _has_payload(message: types.Message) -> bool:
    if (message.text or "").startswith("/"):
        return False
    return bool(
        message.text or message.caption or message.photo or message.document
        or message.video or message.animation or message.audio or message.voice
        or message.video_note or message.sticker or message.location or message.contact
    )


@router.callback_query(F.data.startswith("findw_start:"))
async def findw_start(callback: types.CallbackQuery, state: FSMContext):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    person, error = await gate(callback.from_user.id, tid)
    if error:
        await callback.answer(error, show_alert=True)
        return
    name = html_module.escape(person_name(person))
    await state.set_state(FindWrite.waiting)
    await state.update_data(findw_tid=tid)
    if (person.get("active_messenger") or "").strip() == "vk":
        hint = "Пришлите текст — он уйдёт во ВК. Фото и файл туда не копируются."
    else:
        hint = "Пришлите одним сообщением текст, фото или файл — перешлю как есть."
    await callback.message.edit_text(
        f"✉️ Написать <b>{name}</b>\n\n{hint} Передумали — «Отмена».",
        parse_mode="HTML",
        reply_markup=_cancel_kb(tid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("findw_cancel:"))
async def findw_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.update_data(findw_tid=None)
    await callback.message.edit_text("Отправка отменена.")
    await callback.answer()


@router.message(FindWrite.waiting, F.text.startswith("/"))
async def findw_cancel_text(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(findw_tid=None)
    await message.answer("Отправка отменена.")


@router.message(FindWrite.waiting)
async def findw_receive(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("findw_tid")
    if not isinstance(tid, int):
        await state.set_state(None)
        await message.answer("Не понял, кому писать — откройте карточку заново.")
        return
    if not _has_payload(message):
        await message.answer("Пришлите текст, фото или файл одним сообщением.")
        return
    person, error = await gate(message.from_user.id, tid)
    if error:
        await state.set_state(None)
        await message.answer(error)
        return
    try:
        if (person.get("active_messenger") or "").strip() == "vk":
            body = (message.text or message.caption or "").strip()
            if not body or not person.get("vk_id"):
                await message.answer(
                    "Во ВК уйдёт только текст, и только на связанный аккаунт. "
                    "Пришлите текст."
                )
                return
            await send_to_delegate(tid, body, bot=getattr(message, "bot", None))
        else:
            await message.send_copy(tid)
    except TelegramForbiddenError:
        await state.set_state(None)
        await state.update_data(findw_tid=None)
        await message.answer("Не дошло: человек заблокировал бота.")
        return
    except Exception:
        logger.exception("findw_receive: send %s failed", tid)
        await message.answer("Не смог переслать. Попробуйте ещё раз с карточки.")
        return
    await state.set_state(None)
    await state.update_data(findw_tid=None)
    await mark_sent(message.from_user.id, tid)
    name = html_module.escape(person_name(person))
    await message.answer(f"Отправлено: {name}")
