"""Витрина магазина баллов у делегата: кнопка на экране «🪙 Мои баллы» и экран витрины.

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Делегат сам ничего не покупает: видит баланс, товары с ценами и остатками, сколько не хватает,
и свои покупки. Выдаёт и списывает менеджер на стойке (handlers/game/admin_shop.py). Кнопка
появляется, только пока менеджер открыл витрину и на ней есть товары. Колбэк `shopv` не
админский — в ADMIN_CAPS его нет. Импорт — хвостом handlers/user_actions.py, до фолбэка."""
from __future__ import annotations

import html

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import shop_db
from database.db import get_balance
from handlers.user_actions import router
from services.infra.ru_plural import points_word


async def _title() -> str:
    from handlers.game.admin_shop import shop_title

    return await shop_title()


async def shop_button() -> InlineKeyboardButton | None:
    if not await shop_db.is_open() or not await shop_db.list_items(active_only=True):
        return None
    return InlineKeyboardButton(text=f"🛍 {await _title()}", callback_data="shopv")


async def with_shop_button(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Экран «🪙 Мои баллы» (handlers/user_actions.py::_balance_screen): кнопка витрины первой строкой."""
    button = await shop_button()
    if button is not None:
        kb.inline_keyboard.insert(0, [button])
    return kb


async def showcase_text(telegram_id: int) -> str:
    balance = await get_balance(telegram_id)
    lines = [f"🛍 <b>{html.escape(await _title())}</b>", f"На счету: <b>{balance}</b> {points_word(balance)}", ""]
    for item in await shop_db.list_items(active_only=True):
        line = f"• {html.escape(item['title'])} — {item['price']}"
        if item["stock_left"] is not None and item["stock_left"] <= 0:
            line += " · закончился"
        else:
            if item["stock_left"] is not None:
                line += f" · осталось {item['stock_left']}"
            if item["price"] > balance:
                line += f" · не хватает {item['price'] - balance}"
            if item["per_person"]:
                line += f" · не больше {item['per_person']} в одни руки"
        lines.append(line)
    mine = await shop_db.list_user_sales(telegram_id)
    if mine:
        lines += ["", "<b>Ты уже получил(а)</b>"] + [f"✓ {html.escape(s['title'])}" for s in mine]
    lines += ["", "Подходи к стойке магазина на форуме и назови свой @ник — баллы спишут на месте."]
    return "\n".join(lines)


@router.callback_query(F.data == "shopv")
async def shop_showcase(callback: types.CallbackQuery):
    text = await showcase_text(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="gbal_back")]])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
