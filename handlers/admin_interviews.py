"""Экран менеджера «🗓 Интервью» в разделе «📋 Заявки».

Слоты и брони читает database.interview_db. Своего SQL здесь нет.
Общий слот (город пустой) виден всем. Слот с кодом города — только этому городу.
"""
from __future__ import annotations

from datetime import datetime

from aiogram import F, types
from aiogram.fsm.context import FSMContext

from database import interview_db
from domain.settings.schema import get_setting_typed
from handlers.admin import router
from handlers.states import InterviewAdmin
from services.infra.timeutil import msk_now

_WINDOW_HINT = "Пример: 28.10.2026 10:00. «-» — убрать дату, запись закроется."
_CAP_HINT = "Сколько человек на одно время. Пример: 6"
_DATE_FMT = "%d.%m.%Y %H:%M"
_SLOT_FMT = "%Y-%m-%d %H:%M"
_CANCELLED = "Время интервью {when} отменили. Выбери другое, если запись ещё открыта."
_FIELDS = {
    "window_start": ("interview_window_start", _WINDOW_HINT),
    "window_end": ("interview_window_end", _WINDOW_HINT),
    "cap": ("interview_slot_cap", _CAP_HINT),
    "add": (None, _WINDOW_HINT),
}


def _parse_when(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw.strip(), _DATE_FMT)
    except (TypeError, ValueError):
        return None


def _slot_when(starts_at: str, fmt: str) -> str:
    try:
        return datetime.strptime(starts_at.strip(), _SLOT_FMT).strftime(fmt)
    except (TypeError, ValueError, AttributeError):
        return starts_at


def _person_line(row: dict) -> str:
    name = (row.get("full_name") or "").strip()
    nick = (row.get("username") or "").strip()
    if nick and not nick.startswith("@"):
        nick = "@" + nick
    if name and nick:
        return f"{name} {nick}"
    return name or nick or "имя не указано"


async def _scope(admin_id: int) -> tuple[str | None, bool]:
    """(код города слота, видеть все коды).

    Модуль выключен — слоты общие. «Все города» — видит каждый код, новый слот общий.
    Привязка к городу — слот этого города.
    """
    from domain.cities import ALL_CITIES_LABEL
    from handlers.settings.admin_core import _admin_city_view

    scope, label = await _admin_city_view(admin_id)
    if scope is None and label is None:
        return None, False
    if scope is None or label == ALL_CITIES_LABEL:
        return None, True
    return scope[0], False


async def _visible(admin_id: int, slot: dict | None) -> bool:
    if slot is None:
        return False
    city, every = await _scope(admin_id)
    code = slot.get("event_city")
    if every or not code:
        return True
    return city is not None and code == city


async def _screen(admin_id: int, opened: int | None = None) -> tuple[str, types.InlineKeyboardMarkup]:
    from handlers.settings.admin_sections import back_button

    enabled = await get_setting_typed("interview_booking_enabled") == "on"
    start = await get_setting_typed("interview_window_start")
    end = await get_setting_typed("interview_window_end")
    cap = await get_setting_typed("interview_slot_cap")
    start_label = start.strftime(_DATE_FMT) if isinstance(start, datetime) else "не задано"
    end_label = end.strftime(_DATE_FMT) if isinstance(end, datetime) else "не задано"
    city, every = await _scope(admin_id)
    slots = await interview_db.list_visible_slots(city, every=every)
    rows = [
        [types.InlineKeyboardButton(
            text="Запись: включена" if enabled else "Запись: выключена",
            callback_data="ivadm:toggle",
        )],
        [types.InlineKeyboardButton(
            text=f"С какого момента: {start_label}", callback_data="ivadm:start",
        )],
        [types.InlineKeyboardButton(
            text=f"До какого момента: {end_label}", callback_data="ivadm:end",
        )],
        [types.InlineKeyboardButton(
            text=f"Человек на одно время: {cap}", callback_data="ivadm:cap",
        )],
    ]
    lines = ["🗓 Интервью"]
    for slot in slots:
        taken = await interview_db.slot_taken(slot["id"])
        label = (
            f"{_slot_when(slot['starts_at'], '%d.%m %H:%M')} · "
            f"занято {taken} из {slot['cap']}"
        )
        rows.append([types.InlineKeyboardButton(
            text=label, callback_data=f"ivadm:open:{slot['id']}",
        )])
        if opened is not None and int(slot["id"]) == int(opened):
            people = await interview_db.list_slot_people(slot["id"])
            if people:
                lines.append(label)
                lines.extend(_person_line(p) for p in people)
            else:
                lines.append(f"{label}\nПока никто не записался.")
            rows.append([
                types.InlineKeyboardButton(text="−", callback_data=f"ivadm:dn:{slot['id']}"),
                types.InlineKeyboardButton(text="+", callback_data=f"ivadm:up:{slot['id']}"),
            ])
            rows.append([types.InlineKeyboardButton(
                text="Удалить слот", callback_data=f"ivadm:del:{slot['id']}",
            )])
    rows.append([types.InlineKeyboardButton(text="Добавить время", callback_data="ivadm:add")])
    rows.append([back_button("admin_interviews")])
    return "\n".join(lines), types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _paint(message, admin_id: int, opened: int | None = None, *, edit: bool) -> None:
    text, kb = await _screen(admin_id, opened)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


async def _ask(callback: types.CallbackQuery, state: FSMContext, field: str) -> None:
    await state.set_state(InterviewAdmin.value)
    await state.update_data(iv_field=field)
    await callback.message.answer(_FIELDS[field][1])
    await callback.answer()


@router.callback_query(F.data == "admin_interviews")
async def open_interviews(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _paint(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("ivadm:"))
async def interview_admin_tap(callback: types.CallbackQuery, state: FSMContext, bot):
    admin_id = callback.from_user.id
    data = callback.data
    if data == "ivadm:toggle":
        await _toggle(callback, admin_id)
        return
    if data in ("ivadm:start", "ivadm:end", "ivadm:cap", "ivadm:add"):
        field = {"ivadm:start": "window_start", "ivadm:end": "window_end",
                 "ivadm:cap": "cap", "ivadm:add": "add"}[data]
        await _ask(callback, state, field)
        return
    try:
        action, raw_id = data.split(":")[1], int(data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Не понял кнопку.", show_alert=True)
        return
    slot = await interview_db.get_slot(raw_id)
    if not await _visible(admin_id, slot):
        await callback.answer("Это время другого города.", show_alert=True)
        return
    if action == "open":
        await _paint(callback.message, admin_id, raw_id, edit=True)
        await callback.answer()
    elif action == "up":
        await interview_db.set_slot_cap(raw_id, int(slot["cap"]) + 1)
        await _paint(callback.message, admin_id, raw_id, edit=True)
        await callback.answer()
    elif action == "dn":
        if int(slot["cap"]) <= 1:
            await callback.answer("Ниже 1 нельзя.", show_alert=True)
            return
        await interview_db.set_slot_cap(raw_id, int(slot["cap"]) - 1)
        await _paint(callback.message, admin_id, raw_id, edit=True)
        await callback.answer()
    elif action == "del":
        await _delete_ask(callback, slot)
    elif action == "delok":
        await _delete_go(callback, bot, slot)
    else:
        await callback.answer()


async def _toggle(callback: types.CallbackQuery, admin_id: int) -> None:
    from domain.settings.ops import COMMON_DENIED_TEXT
    from services.settings.audit import CommonSettingDenied, set_setting_by_admin

    current = await get_setting_typed("interview_booking_enabled")
    new = "off" if current == "on" else "on"
    try:
        await set_setting_by_admin(admin_id, "interview_booking_enabled", new)
    except CommonSettingDenied:
        await callback.answer(COMMON_DENIED_TEXT, show_alert=True)
        return
    await _paint(callback.message, admin_id, edit=True)
    await callback.answer("Запись включена" if new == "on" else "Запись выключена")


async def _delete_ask(callback: types.CallbackQuery, slot: dict) -> None:
    people = await interview_db.list_slot_people(slot["id"])
    if not people:
        await interview_db.delete_slot(slot["id"])
        await _paint(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Время удалено")
        return
    n = len(people)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="Удалить", callback_data=f"ivadm:delok:{slot['id']}",
        )],
        [types.InlineKeyboardButton(text="Отмена", callback_data="admin_interviews")],
    ])
    await callback.message.edit_text(
        f"Запись пропадёт у {n} человек. Им уйдёт сообщение, что это время отменили.",
        reply_markup=kb,
    )
    await callback.answer()


async def _delete_go(callback: types.CallbackQuery, bot, slot: dict) -> None:
    when = _slot_when(slot["starts_at"], _DATE_FMT)
    raw = await get_setting_typed("interview_slot_cancelled_text")
    text = (raw if isinstance(raw, str) and raw.strip() else _CANCELLED).replace("{when}", when)
    users = await interview_db.delete_slot(slot["id"])
    for uid in users:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass
    await _paint(callback.message, callback.from_user.id, edit=True)
    await callback.answer("Время удалено")


@router.message(InterviewAdmin.value)
async def interview_admin_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("iv_field")
    if field not in _FIELDS:
        await state.clear()
        await message.answer("Сессия истекла. Откройте «🗓 Интервью» ещё раз.")
        return
    raw = (message.text or "").strip()
    key, hint = _FIELDS[field]
    if field in ("window_start", "window_end"):
        await _save_window(message, state, key, raw, hint)
        return
    if field == "cap":
        await _save_cap(message, state, raw, hint)
        return
    await _save_slot(message, state, raw, hint)


async def _save_window(message, state, key: str, raw: str, hint: str) -> None:
    from domain.settings.ops import COMMON_DENIED_TEXT
    from services.settings.audit import CommonSettingDenied, set_setting_by_admin

    if raw in ("-", "—", "«-»"):
        value = ""
    else:
        parsed = _parse_when(raw)
        if parsed is None:
            await message.answer(hint)
            return
        value = parsed.strftime(_DATE_FMT)
    try:
        await set_setting_by_admin(message.from_user.id, key, value)
    except CommonSettingDenied:
        await message.answer(COMMON_DENIED_TEXT)
        return
    await state.clear()
    await _paint(message, message.from_user.id, edit=False)


async def _save_cap(message, state, raw: str, hint: str) -> None:
    from domain.settings.ops import COMMON_DENIED_TEXT
    from services.settings.audit import CommonSettingDenied, set_setting_by_admin

    try:
        number = int(raw)
    except ValueError:
        number = 0
    if number < 1:
        await message.answer(hint)
        return
    try:
        await set_setting_by_admin(message.from_user.id, "interview_slot_cap", str(number))
    except CommonSettingDenied:
        await message.answer(COMMON_DENIED_TEXT)
        return
    await state.clear()
    await _paint(message, message.from_user.id, edit=False)


async def _save_slot(message, state, raw: str, hint: str) -> None:
    parsed = _parse_when(raw)
    if parsed is None:
        await message.answer(hint)
        return
    cap = await get_setting_typed("interview_slot_cap")
    cap = int(cap) if isinstance(cap, int) and cap >= 1 else 1
    city, _every = await _scope(message.from_user.id)
    await interview_db.create_slot(
        city, parsed.strftime(_SLOT_FMT), cap, message.from_user.id,
        msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    await state.clear()
    await _paint(message, message.from_user.id, edit=False)
