"""«✉️ Написать» с карточки /find — одно сообщение менеджера копируется делегату."""
from __future__ import annotations

import html as html_module
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_reg_started_by_id, get_user, record_admin_action
from domain.cities import normalize_city
from handlers.admin import router
from handlers.forum.admin_checkin import _city_allowed
from handlers.states import FindWrite

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN = "Этот город вне вашей зоны ответственности."
_NOT_FOUND = "Человек не найден — карточка устарела."


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _cancel_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"findw_cancel:{tid}")],
    ])


async def _load(tid: int) -> tuple[dict | None, str | None]:
    user = await get_user(tid)
    if user is not None:
        return user, normalize_city(user.get("event_city"))
    started = await get_reg_started_by_id(tid)
    if started is not None:
        return started, normalize_city(started.get("event_city"))
    return None, None


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
    person, city = await _load(tid)
    if person is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, city):
        await callback.answer(_CITY_FORBIDDEN, show_alert=True)
        return
    name = html_module.escape(str(person.get("full_name") or tid))
    await state.set_state(FindWrite.waiting)
    await state.update_data(findw_tid=tid)
    await callback.message.edit_text(
        f"✉️ Написать <b>{name}</b>\n\n"
        "Пришлите одним сообщением текст, фото или файл — перешлю как есть. "
        "Передумали — «Отмена».",
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
    person, city = await _load(tid)
    if person is None:
        await state.set_state(None)
        await message.answer(_NOT_FOUND)
        return
    if not await _city_allowed(message.from_user.id, city):
        await state.set_state(None)
        await message.answer(_CITY_FORBIDDEN)
        return
    try:
        await message.send_copy(tid)
    except TelegramForbiddenError:
        await state.set_state(None)
        await state.update_data(findw_tid=None)
        await message.answer("Не дошло: человек заблокировал бота.")
        return
    except Exception:
        logger.exception("findw_receive: send_copy %s failed", tid)
        await message.answer("Не смог переслать. Попробуйте ещё раз с карточки.")
        return
    await state.set_state(None)
    await state.update_data(findw_tid=None)
    await record_admin_action(
        actor_id=message.from_user.id, subject_id=tid, action="write_message",
    )
    name = html_module.escape(str(person.get("full_name") or tid))
    await message.answer(f"Отправлено: {name}")
