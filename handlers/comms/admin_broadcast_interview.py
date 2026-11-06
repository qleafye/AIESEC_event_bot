"""Галочка «кнопка выбора времени» на подтверждении рассылки.

Текст письма галочка не пишет: его менеджер уже ввёл в мастере.
Пока галочка выключена, исходящее сообщение остаётся прежним.
Отложенная рассылка кладёт флаг рядом с фильтром и не ломает старый список фильтров.
"""
from __future__ import annotations

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from domain.settings.schema import get_setting_typed
from handlers.admin import router
from handlers.states import Broadcast
from services.comms.broadcast_interview import (
    pack_filter_spec,
    peel_interview_flag,
    with_interview_button,
)

_CHECK_OFF = "🗓 Кнопка «Выбрать время»"
_CHECK_ON = "✅ 🗓 Кнопка «Выбрать время»"


async def confirm_rows(state: FSMContext) -> list:
    """Один ряд на подтверждении. Пусто, пока запись на интервью выключена."""
    if await get_setting_typed("interview_booking_enabled") != "on":
        return []
    on = bool((await state.get_data()).get("bc_interview_button"))
    return [[InlineKeyboardButton(
        text=_CHECK_ON if on else _CHECK_OFF, callback_data="bciv_toggle",
    )]]


@router.callback_query(F.data == "bciv_toggle", StateFilter(Broadcast.confirm, Broadcast.schedule_confirm))
async def bciv_toggle(callback: types.CallbackQuery, state: FSMContext, bot):
    data = await state.get_data()
    on = not bool(data.get("bc_interview_button"))
    await state.update_data(bc_interview_button=on)
    await callback.answer(
        "Кнопка «Выбрать время» будет в сообщении" if on else "Кнопка снята",
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    current = await state.get_state()
    if current == Broadcast.schedule_confirm.state:
        from handlers.comms.admin_broadcasts import _send_schedule_confirm_prompt
        await _send_schedule_confirm_prompt(callback.message, state)
        return
    from handlers.comms.admin_broadcasts import _send_confirm_prompt
    users = data.get("bc_users", [])
    await _send_confirm_prompt(bot, callback.message.chat.id, state, len(users), users)
