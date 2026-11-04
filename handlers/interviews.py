"""Сетка времени для делегата. Вход — кнопка под рассылкой, не пункт меню.

Имена записанных сюда не попадают: делегат видит только «осталось N».
"""
from __future__ import annotations

from datetime import datetime

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import interview_db
from database.db import get_user
from domain.settings.schema import get_setting_typed
from handlers.user_actions import router
from services import interviews
from services.infra.timeutil import msk_now

_SLOT_FMT = "%Y-%m-%d %H:%M"
_WHEN = "%d.%m.%Y %H:%M"
_NONE_LEFT = "Свободного времени не осталось"
_TAKEN_LINE = "Часть времени уже занята."
_MOVE = "Перенести"


def _parse(starts_at: str) -> datetime | None:
    try:
        return datetime.strptime(starts_at.strip(), _SLOT_FMT)
    except (TypeError, ValueError, AttributeError):
        return None


def _when(starts_at: str) -> str:
    parsed = _parse(starts_at)
    return parsed.strftime(_WHEN) if parsed else starts_at


async def _phrase(key: str, fallback: str, when: str | None = None) -> str:
    raw = await get_setting_typed(key)
    text = raw if isinstance(raw, str) and raw.strip() else fallback
    return text.replace("{when}", when) if when else text


async def _grid(user: dict, *, move: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    reason = await interviews.eligible(user)
    if reason:
        return reason, None
    now = msk_now()
    closed = await interviews.window_open(now)
    if closed:
        return closed, None
    if move:
        booking = await interview_db.get_booking(int(user["telegram_id"]))
        if booking is None:
            return await _phrase("interview_closed_text", "Запись закрыта."), None
        if int(booking["reschedule_count"]) != 0:
            return await _phrase(
                "interview_reschedule_spent_text", "Перенести можно было один раз.",
            ), None
        current = int(booking["slot_id"])
    else:
        current = None
    city = (user.get("event_city") or "").strip() or None
    slots = await interview_db.list_visible_slots(city, every=False)
    buttons = []
    taken_any = False
    for slot in slots:
        starts = _parse(slot["starts_at"])
        if starts is None or starts <= now:
            continue
        if current is not None and int(slot["id"]) == current:
            continue
        taken = await interview_db.slot_taken(slot["id"])
        left = int(slot["cap"]) - taken
        if left <= 0:
            taken_any = True
            continue
        prefix = "iv_moveto" if move else "iv_pick"
        buttons.append([InlineKeyboardButton(
            text=f"{starts.strftime('%d.%m %H:%M')} · осталось {left}",
            callback_data=f"{prefix}:{slot['id']}",
        )])
    if not buttons:
        text = _NONE_LEFT
        if taken_any:
            text = f"{text}\n{_TAKEN_LINE}"
        return text, None
    lines = ["Выбери время"]
    if taken_any:
        lines.append(_TAKEN_LINE)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show(callback: types.CallbackQuery, text: str, kb, *, fresh: bool) -> None:
    if fresh:
        await callback.message.answer(text, reply_markup=kb)
    else:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


async def _user(callback: types.CallbackQuery) -> dict | None:
    return await get_user(callback.from_user.id)


@router.callback_query(F.data == "iv_open")
async def interview_open(callback: types.CallbackQuery):
    user = await _user(callback)
    if user is None:
        await _show(callback, "Сначала подай заявку.", None, fresh=True)
        return
    text, kb = await _grid(user, move=False)
    await _show(callback, text, kb, fresh=True)


@router.callback_query(F.data.startswith("iv_pick:"))
async def interview_pick(callback: types.CallbackQuery):
    user = await _user(callback)
    if user is None:
        await _show(callback, "Сначала подай заявку.", None, fresh=False)
        return
    try:
        slot_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    err = await interviews.book_slot(user, slot_id)
    if err:
        await _show(callback, err, None, fresh=False)
        return
    slot = await interview_db.get_slot(slot_id)
    booking = await interview_db.get_booking(int(user["telegram_id"]))
    text = await _phrase("interview_booked_text", "Ты записан на интервью {when}.", _when(slot["starts_at"]))
    kb = None
    if booking and int(booking["reschedule_count"]) == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=_MOVE, callback_data="iv_move"),
        ]])
    await _show(callback, text, kb, fresh=False)


@router.callback_query(F.data == "iv_move")
async def interview_move(callback: types.CallbackQuery):
    user = await _user(callback)
    if user is None:
        await _show(callback, "Сначала подай заявку.", None, fresh=False)
        return
    text, kb = await _grid(user, move=True)
    await _show(callback, text, kb, fresh=False)


@router.callback_query(F.data.startswith("iv_moveto:"))
async def interview_move_to(callback: types.CallbackQuery):
    user = await _user(callback)
    if user is None:
        await _show(callback, "Сначала подай заявку.", None, fresh=False)
        return
    try:
        slot_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    err = await interviews.move_slot(user, slot_id)
    if err:
        await _show(callback, err, None, fresh=False)
        return
    slot = await interview_db.get_slot(slot_id)
    text = await _phrase(
        "interview_moved_text", "Интервью перенесено на {when}.", _when(slot["starts_at"]),
    )
    await _show(callback, text, None, fresh=False)
