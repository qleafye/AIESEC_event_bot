"""Кнопка согласия на подтверждении рассылки.

Текст письма менеджер пишет сам. Список — только те согласия, которые анкета не спрашивает.
К альбому кнопка не крепится.
"""
from __future__ import annotations

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.regform.engine import broadcast_consent_entries
from handlers.admin import router
from handlers.states import Broadcast
from services.comms.broadcast_consent import consent_button, with_consent_button

_OPEN = "📸 Кнопка согласия"
_CLEAR = "Убрать кнопку"
_EMPTY_ALERT = (
    "Сначала добавьте согласие для рассылки в разделе согласий. Анкета его не спросит."
)
_LONG_ALERT = "Сократите ключ согласия: он не помещается на кнопку"
ALBUM_NOTE = "К альбому кнопка не крепится, письмо ушло без неё."
_CALLBACK_LIMIT = 64


def _choice_data(key: str) -> str:
    data = f"bc_consent:{key}"
    if len(data.encode()) >= _CALLBACK_LIMIT:
        return "bc_consent_long"
    return data


async def confirm_row(state: FSMContext) -> list:
    """Один ряд на подтверждении. Пусто — «Кнопка согласия», выбрано — название и снятие."""
    key = ((await state.get_data()).get("bc_consent_key") or "").strip()
    if not key:
        return [[InlineKeyboardButton(text=_OPEN, callback_data="bc_consent_open")]]
    entries = await broadcast_consent_entries()
    label = next((lbl for lbl, item in entries if item == key), key)
    return [[
        InlineKeyboardButton(text=f"✅ Кнопка: {label}", callback_data="bc_consent_open"),
        InlineKeyboardButton(text=_CLEAR, callback_data="bc_consent_clear"),
    ]]


async def consent_tap_counts(ids) -> dict[int, tuple[int, int]]:
    """N — нажатия, M — broadcasts.delivered."""
    clean = []
    for item in ids or []:
        try:
            clean.append(int(item))
        except (TypeError, ValueError):
            continue
    if not clean:
        return {}
    from database.db import _connect

    marks = ",".join("?" * len(clean))
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, delivered FROM broadcasts WHERE id IN ({marks})", tuple(clean),
        ) as cursor:
            delivered = {row[0]: int(row[1] or 0) for row in await cursor.fetchall()}
        async with conn.execute(
            "SELECT broadcast_id, COUNT(*) FROM broadcast_consent_taps "
            f"WHERE broadcast_id IN ({marks}) GROUP BY broadcast_id",
            tuple(clean),
        ) as cursor:
            taps = {row[0]: int(row[1]) for row in await cursor.fetchall()}
    return {item: (taps.get(item, 0), delivered.get(item, 0)) for item in clean}


async def attach_consent_lines(rows: list[dict]) -> None:
    """Поле consent_line на строке журнала. Пустая строка, если кнопки не было."""
    counts = await consent_tap_counts([row.get("id") for row in rows])
    for row in rows:
        key = (row.get("consent_key") or "").strip()
        if not key:
            row["consent_line"] = ""
            continue
        n, delivered = counts.get(int(row["id"]), (0, int(row.get("delivered") or 0)))
        row["consent_line"] = f"Нажали кнопку: {n} из {delivered}"


async def _redraw(callback: types.CallbackQuery, state: FSMContext, bot) -> None:
    current = await state.get_state()
    if current == Broadcast.schedule_confirm.state:
        from handlers.comms.admin_broadcasts import _send_schedule_confirm_prompt
        await _send_schedule_confirm_prompt(callback.message, state)
        return
    from handlers.comms.admin_broadcasts import _send_confirm_prompt
    users = (await state.get_data()).get("bc_users", [])
    await _send_confirm_prompt(bot, callback.message.chat.id, state, len(users), users)


@router.callback_query(F.data == "bc_consent_open", StateFilter(Broadcast.confirm, Broadcast.schedule_confirm))
async def bc_consent_open(callback: types.CallbackQuery, state: FSMContext):
    entries = await broadcast_consent_entries()
    if not entries:
        await callback.answer(_EMPTY_ALERT, show_alert=True)
        return
    rows = []
    for label, key in entries:
        rows.append([InlineKeyboardButton(text=label, callback_data=_choice_data(key))])
    await callback.message.edit_text(
        "Какое согласие прикрепить к письму?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data == "bc_consent_long", StateFilter(Broadcast.confirm, Broadcast.schedule_confirm))
async def bc_consent_long(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer(_LONG_ALERT, show_alert=True)


@router.callback_query(F.data.startswith("bc_consent:"), StateFilter(Broadcast.confirm, Broadcast.schedule_confirm))
async def bc_consent_pick(callback: types.CallbackQuery, state: FSMContext, bot):
    if len(callback.data.encode()) >= _CALLBACK_LIMIT:
        await callback.answer(_LONG_ALERT, show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    entries = await broadcast_consent_entries()
    if key not in {item for _label, item in entries}:
        await callback.answer(_EMPTY_ALERT, show_alert=True)
        return
    await state.update_data(bc_consent_key=key)
    await callback.answer("Кнопка согласия будет в письме")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _redraw(callback, state, bot)


@router.callback_query(F.data == "bc_consent_clear", StateFilter(Broadcast.confirm, Broadcast.schedule_confirm))
async def bc_consent_clear(callback: types.CallbackQuery, state: FSMContext, bot):
    await state.update_data(bc_consent_key="")
    await callback.answer("Кнопка снята")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _redraw(callback, state, bot)
