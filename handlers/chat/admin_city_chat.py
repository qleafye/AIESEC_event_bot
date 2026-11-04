"""Экран «Чат города»: как людей пускают, срок, лимит и одна сводка.

Текст режима — ответ describe, как Telegram ответил сейчас. Открытие ссылку не
создаёт. Пока ссылка уже выдана, смена режима, лимита или срока сначала
спрашивает подтверждение: старая ссылка после смены перестаёт пускать.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import city_chat_db
from domain.cities import ALL_CITIES, admin_selected_city, cities_module_on, city_label
from domain.settings.ops import per_city_visible_codes
from handlers.admin import router
from handlers.chat.city_chat_api import client_from_bot
from handlers.chat.city_chat_summary_send import send_summary
from handlers.states import CityChatEdit
from services.chat_tracking import bound_chats, chat_for_city
from services.city_chat_link import describe, set_policy

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test): every `@router.*` decorator below MUST fit on ONE line.

_GLOBAL = "g"
_ASKS = "creates_" + "join_request"
_JOIN_MARK = "пускают по заявке"
_OPEN_MARK = "пускает сразу"
_NO_LINK_MARK = "Ссылки ещё нет"
_FOREIGN = "Это чат другого города"
_CONFIRM = "Ссылка сменится, старая перестанет пускать. В новых письмах будет новая."
_LIMIT_BLOCK = (
    "Telegram не разрешает лимит мест вместе с заявками. "
    "Сначала выберите «Пускать сразу»."
)
_BAD_NUMBER = "Нужно целое число, например 14."
_LIMIT_PROMPT = "Напишите, сколько человек может пустить эта ссылка, например 200"
_DAYS_PROMPT = "Напишите срок в днях, например 14"
_PICK = "Выберите чат города."
_NONE_BOUND = "Ни один чат города ещё не подключён."
_UNBOUND = "Чат этого города ещё не подключён."
_STALE = "Кнопка устарела"
_CHECK_FAILED = "Не смог проверить ссылку в чат. Старую ссылку не отправил."


def _log(city, exc: Exception) -> None:
    logger.warning("admin_city_chat: city=%s exc=%s", city or "", type(exc).__name__)


def _tail(data: str) -> str:
    parts = (data or "").split(":", 2)
    return parts[2] if len(parts) == 3 else ""


def _whole(text) -> int | None:
    raw = (text or "").strip().replace(" ", "")
    if not raw.isdigit():
        return None
    return int(raw)


def _join_from(phrase: str, row) -> bool:
    text = phrase or ""
    if _JOIN_MARK in text:
        return True
    if _OPEN_MARK in text:
        return False
    if not row or not row.get("invite_link"):
        return True
    return bool(row.get(_ASKS, 1))


def _has_link(phrase: str, row) -> bool:
    if (phrase or "").startswith(_NO_LINK_MARK):
        return False
    return bool(row and row.get("invite_link"))


def _span(row, key: str, low: int, high: int):
    if not row:
        return None
    value = row.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if low <= number <= high:
        return number
    return None


def _btn(text: str, data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=data)]


async def _allowed(admin_id: int, token: str) -> tuple[bool, str | None]:
    """Город экрана против шапки. `*` и `g` в normalize_city не попадают."""
    if not await cities_module_on():
        return token == _GLOBAL, None
    if not token or token in (_GLOBAL, ALL_CITIES):
        return False, None
    if token not in await per_city_visible_codes(admin_id):
        return False, None
    header = await admin_selected_city(admin_id)
    if header != ALL_CITIES and header != token:
        return False, None
    return True, token


async def _show_switch(admin_id: int) -> bool:
    if not await cities_module_on():
        return False
    header = await admin_selected_city(admin_id)
    if not header:
        return False
    if header == ALL_CITIES:
        return True
    return len(await per_city_visible_codes(admin_id)) != 1


async def _nav_rows(admin_id: int) -> list[list[InlineKeyboardButton]]:
    rows = []
    if await _show_switch(admin_id):
        rows.append(_btn("🏙 Сменить город", "admin_city_switch:manage"))
    rows.append(_btn("Назад", "admin_sec:manage"))
    return rows


def _confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn("Да, сменить", f"cchat:apply:{token}"),
        _btn("Не менять", f"cchat:stay:{token}"),
    ])


async def _mode_kb(admin_id: int, token: str) -> InlineKeyboardMarkup:
    rows = [
        _btn("Пускать по заявке", f"cchat:req:{token}"),
        _btn("Пускать сразу", f"cchat:now:{token}"),
        _btn("Без лимита мест", f"cchat:lnone:{token}"),
        _btn("Задать лимит", f"cchat:lask:{token}"),
        _btn("Без срока", f"cchat:dnone:{token}"),
        _btn("Задать срок", f"cchat:dask:{token}"),
        _btn("Сводка", f"cchat:sum:{token}"),
    ]
    rows.extend(await _nav_rows(admin_id))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _unbound_view(admin_id: int):
    kb = InlineKeyboardMarkup(inline_keyboard=await _nav_rows(admin_id))
    return _UNBOUND, kb


async def _list(admin_id: int):
    rows = []
    for entry in await bound_chats():
        code = entry.get("city")
        if not code:
            continue
        rows.append(_btn(await city_label(code), f"cchat:open:{code}"))
    text = _PICK if rows else _NONE_BOUND
    rows.extend(await _nav_rows(admin_id))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _phrase(bot, city) -> str:
    try:
        return await describe(client_from_bot(bot), city)
    except Exception as exc:
        _log(city, exc)
        return _CHECK_FAILED


async def _one_city(admin_id: int, token: str, city, bot):
    chat = await chat_for_city(city)
    if not chat:
        return await _unbound_view(admin_id)
    phrase = await _phrase(bot, city)
    title = (chat.get("title") or "").strip()
    text = phrase if not title else f"{phrase}\n\n{html.escape(title)}"
    return text, await _mode_kb(admin_id, token)


async def render_city_chat_screen(admin_id: int, token: str | None = None, bot=None):
    if not await cities_module_on():
        return await _one_city(admin_id, _GLOBAL, None, bot)
    header = await admin_selected_city(admin_id)
    if token is None:
        if header == ALL_CITIES:
            return await _list(admin_id)
        token = header
    if token in (None, ALL_CITIES):
        return await _list(admin_id)
    if token == _GLOBAL:
        return await _one_city(admin_id, _GLOBAL, None, bot)
    return await _one_city(admin_id, token, token, bot)


async def _paint(message, text, kb, *, edit: bool) -> None:
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _apply(bot, city, phrase: str, spec: dict):
    row = await city_chat_db.get_link(city)
    join = _join_from(phrase, row)
    days = _span(row, "lifetime_days", 1, 365)
    limit = None if join else _span(row, "member_limit", 1, 99999)
    pending = spec.get("pending")
    if pending == "join":
        join, limit = True, None
    elif pending == "open":
        join = False
    elif pending == "nolimit":
        limit = None
    elif pending == "nodays":
        days = None
    elif pending == "limit":
        join = False
        limit = int(spec["number"])
    elif pending == "days":
        days = int(spec["number"])
        if join:
            limit = None
    return await set_policy(
        client_from_bot(bot),
        city,
        join_requests=join,
        member_limit=limit,
        lifetime_days=days,
    )


async def _commit(message, admin_id: int, bot, state: FSMContext, city, phrase: str, spec: dict, *, edit: bool) -> None:
    result = None
    try:
        result = await _apply(bot, city, phrase, spec)
    except Exception as exc:
        _log(city, exc)
    await state.set_state(None)
    await state.update_data(token=spec.get("token"), pending=None, number=None)
    if result is not None and result.ok:
        text, kb = await render_city_chat_screen(admin_id, spec.get("token"), bot)
    else:
        err = (result.error if result is not None else None) or _CHECK_FAILED
        text, kb = err, await _mode_kb(admin_id, spec.get("token") or _GLOBAL)
    await _paint(message, text, kb, edit=edit)


async def _refuse(callback: types.CallbackQuery) -> None:
    await callback.answer(_FOREIGN, show_alert=True)


async def _gate(callback: types.CallbackQuery, token: str):
    ok, city = await _allowed(callback.from_user.id, token)
    if not ok:
        await _refuse(callback)
        return False, None
    return True, city


async def _ensure_chat(callback: types.CallbackQuery, city) -> bool:
    if await chat_for_city(city):
        return True
    text, kb = await _unbound_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()
    return False


async def _stage(callback: types.CallbackQuery, state: FSMContext, token: str, city, pending: str) -> None:
    phrase = await _phrase(callback.bot, city)
    row = await city_chat_db.get_link(city)
    if _has_link(phrase, row):
        await state.set_state(None)
        await state.update_data(token=token, pending=pending, number=None)
        await callback.message.edit_text(_CONFIRM, reply_markup=_confirm_kb(token))
        await callback.answer()
        return
    spec = {"token": token, "pending": pending}
    await _commit(
        callback.message, callback.from_user.id, callback.bot, state, city, phrase, spec, edit=True,
    )
    await callback.answer()


async def _change(callback: types.CallbackQuery, state: FSMContext, pending: str) -> None:
    token = _tail(callback.data)
    ok, city = await _gate(callback, token)
    if not ok or not await _ensure_chat(callback, city):
        return
    await _stage(callback, state, token, city, pending)


@router.callback_query(F.data == "admin_city_chat")
async def city_chat_open(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_city_chat_screen(callback.from_user.id, None, callback.bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cchat:open:"))
async def city_chat_pick(callback: types.CallbackQuery, state: FSMContext):
    token = _tail(callback.data)
    ok, _city = await _gate(callback, token)
    if not ok:
        return
    await state.clear()
    text, kb = await render_city_chat_screen(callback.from_user.id, token, callback.bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cchat:req:"))
async def city_chat_join_mode(callback: types.CallbackQuery, state: FSMContext):
    await _change(callback, state, "join")


@router.callback_query(F.data.startswith("cchat:now:"))
async def city_chat_open_mode(callback: types.CallbackQuery, state: FSMContext):
    await _change(callback, state, "open")


@router.callback_query(F.data.startswith("cchat:lnone:"))
async def city_chat_no_limit(callback: types.CallbackQuery, state: FSMContext):
    await _change(callback, state, "nolimit")


@router.callback_query(F.data.startswith("cchat:dnone:"))
async def city_chat_no_days(callback: types.CallbackQuery, state: FSMContext):
    await _change(callback, state, "nodays")


@router.callback_query(F.data.startswith("cchat:lask:"))
async def city_chat_ask_limit(callback: types.CallbackQuery, state: FSMContext):
    token = _tail(callback.data)
    ok, city = await _gate(callback, token)
    if not ok or not await _ensure_chat(callback, city):
        return
    phrase = await _phrase(callback.bot, city)
    if _JOIN_MARK in phrase:
        await callback.answer(_LIMIT_BLOCK, show_alert=True)
        return
    await state.set_state(CityChatEdit.waiting_for_limit)
    await state.update_data(token=token, pending="limit", number=None)
    await callback.message.edit_text(
        _LIMIT_PROMPT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_btn("Назад", f"cchat:open:{token}")]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cchat:dask:"))
async def city_chat_ask_days(callback: types.CallbackQuery, state: FSMContext):
    token = _tail(callback.data)
    ok, city = await _gate(callback, token)
    if not ok or not await _ensure_chat(callback, city):
        return
    await state.set_state(CityChatEdit.waiting_for_days)
    await state.update_data(token=token, pending="days", number=None)
    await callback.message.edit_text(
        _DAYS_PROMPT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_btn("Назад", f"cchat:open:{token}")]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cchat:sum:"))
async def city_chat_summary(callback: types.CallbackQuery):
    token = _tail(callback.data)
    ok, city = await _gate(callback, token)
    if not ok or not await _ensure_chat(callback, city):
        return
    chat = await chat_for_city(city)
    await send_summary(callback.bot, chat["chat_id"], callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("cchat:apply:"))
async def city_chat_apply(callback: types.CallbackQuery, state: FSMContext):
    token = _tail(callback.data)
    ok, city = await _gate(callback, token)
    if not ok:
        return
    data = await state.get_data()
    pending = data.get("pending")
    if data.get("token") != token or not pending:
        await callback.answer(_STALE, show_alert=True)
        return
    if not await _ensure_chat(callback, city):
        return
    phrase = await _phrase(callback.bot, city)
    spec = {"token": token, "pending": pending}
    if data.get("number") is not None:
        spec["number"] = data["number"]
    await _commit(
        callback.message, callback.from_user.id, callback.bot, state, city, phrase, spec, edit=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cchat:stay:"))
async def city_chat_stay(callback: types.CallbackQuery, state: FSMContext):
    token = _tail(callback.data)
    ok, _city = await _gate(callback, token)
    if not ok:
        return
    await state.set_state(None)
    await state.update_data(token=token, pending=None, number=None)
    text, kb = await render_city_chat_screen(callback.from_user.id, token, callback.bot)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _on_number(message: types.Message, state: FSMContext, kind: str, low: int, high: int) -> None:
    data = await state.get_data()
    token = str(data.get("token") or "")
    ok, city = await _allowed(message.from_user.id, token)
    if not ok:
        await message.answer(_FOREIGN)
        return
    number = _whole(getattr(message, "text", None))
    if number is None or number < low or number > high:
        await message.answer(_BAD_NUMBER)
        return
    if not await chat_for_city(city):
        await message.answer(_UNBOUND)
        return
    phrase = await _phrase(message.bot, city)
    row = await city_chat_db.get_link(city)
    spec = {"token": token, "pending": kind, "number": number}
    if _has_link(phrase, row):
        await state.set_state(None)
        await state.update_data(**spec)
        await message.answer(_CONFIRM, reply_markup=_confirm_kb(token))
        return
    await _commit(
        message, message.from_user.id, message.bot, state, city, phrase, spec, edit=False,
    )


@router.message(StateFilter(CityChatEdit.waiting_for_limit))
async def city_chat_limit_value(message: types.Message, state: FSMContext):
    await _on_number(message, state, "limit", 1, 99999)


@router.message(StateFilter(CityChatEdit.waiting_for_days))
async def city_chat_days_value(message: types.Message, state: FSMContext):
    await _on_number(message, state, "days", 1, 365)
