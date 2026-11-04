"""Кнопки сводки чата города: оставить в чате или убрать после подтверждения.

Модуль вешает хендлеры на общий admin.router. Хвост handlers/admin.py подключит его
отдельной строкой; до этого тесты импортируют модуль сами.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import city_chat_db
from handlers.admin import router
from handlers.chat.city_chat_summary_send import PAGE, summary_keyboard, summary_text
from services.chat_tracking import bound_chats
from services.city_chat_gate import outsider_rows
from services.city_chat_link import recipients_for_city

logger = logging.getLogger(__name__)

_ACTIONS = {"keep", "ask", "yes", "no", "page"}


def _parse(data: str):
    parts = (data or "").split(":", 3)
    if len(parts) != 4 or parts[0] != "cchat" or parts[1] not in _ACTIONS:
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except (TypeError, ValueError):
        return None


async def _binding(chat_id: int):
    for entry in await bound_chats():
        if entry["chat_id"] == int(chat_id):
            return entry
    return None


def _title(entry) -> str:
    raw = (entry.get("title") or "").strip()
    return raw or "чат города"


def _person(rows, telegram_id: int):
    for row in rows:
        if row["telegram_id"] == int(telegram_id):
            return row
    return None


def _ban_phrase(exc: Exception) -> str:
    text = str(exc).lower()
    if "administrator" in text or "admin of the chat" in text:
        return "Это администратор чата, бот его не исключит."
    return "У бота нет права исключать участников."


async def _paint(callback, entry, chat_id: int, page: int = 0) -> None:
    rows = await outsider_rows(chat_id)
    pages = max(1, (len(rows) + PAGE - 1) // PAGE) if rows else 1
    shown_page = min(max(int(page), 0), pages - 1)
    markup = summary_keyboard(chat_id, rows, shown_page)
    shown = rows[shown_page * PAGE:(shown_page + 1) * PAGE] if rows else []
    await callback.message.edit_text(
        summary_text(_title(entry), shown),
        parse_mode="HTML",
        reply_markup=markup,
    )


async def _allowed(callback, chat_id: int):
    entry = await _binding(chat_id)
    if entry is None:
        await callback.answer("Кнопка устарела", show_alert=True)
        return None
    if callback.from_user.id not in await recipients_for_city(entry["city"]):
        await callback.answer("Это чат другого города", show_alert=True)
        return None
    return entry


@router.callback_query(F.data.startswith("cchat:keep:"))
@router.callback_query(F.data.startswith("cchat:ask:"))
@router.callback_query(F.data.startswith("cchat:yes:"))
@router.callback_query(F.data.startswith("cchat:no:"))
@router.callback_query(F.data.startswith("cchat:page:"))
async def on_summary_button(callback: types.CallbackQuery, bot: Bot) -> None:
    parsed = _parse(callback.data)
    if parsed is None:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    action, chat_id, target = parsed
    entry = await _allowed(callback, chat_id)
    if entry is None:
        return
    if action == "page":
        await _paint(callback, entry, chat_id, target)
        await callback.answer()
        return
    rows = await outsider_rows(chat_id)
    person = _person(rows, target)
    if action == "keep":
        if person is not None:
            await city_chat_db.allow(chat_id, target, added_by=callback.from_user.id)
        await _paint(callback, entry, chat_id)
        await callback.answer()
        return
    if action == "ask":
        if person is None:
            await callback.answer("Этого человека в сводке уже нет.", show_alert=True)
            return
        title = html.escape(_title(entry))
        name = html.escape(person["name"])
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Да, убрать из чата",
                callback_data=f"cchat:yes:{chat_id}:{target}",
            )],
            [InlineKeyboardButton(
                text="Не убирать",
                callback_data=f"cchat:no:{chat_id}:{target}",
            )],
        ])
        await callback.message.edit_text(
            f"Убрать {name} из чата «{title}»?",
            parse_mode="HTML",
            reply_markup=markup,
        )
        await callback.answer()
        return
    if action == "no":
        await _paint(callback, entry, chat_id)
        await callback.answer()
        return
    if person is None:
        await callback.answer("Этого человека в сводке уже нет.", show_alert=True)
        return
    name = html.escape(person["name"])
    title = html.escape(_title(entry))
    try:
        await bot.ban_chat_member(chat_id, target)
    except Exception as exc:
        phrase = _ban_phrase(exc)
        await callback.message.edit_text(phrase)
        await callback.answer(phrase, show_alert=True)
        return
    try:
        await bot.unban_chat_member(chat_id, target)
    except Exception as exc:
        logger.warning(
            "city_chat_summary: запрет не снял, чат %s: %s",
            chat_id, type(exc).__name__,
        )
        await callback.message.edit_text(
            f"Убрал {name} из чата «{title}». Вернуться по заявке пока нельзя."
        )
        await callback.answer()
        return
    await callback.message.edit_text(f"Убрал {name} из чата «{title}».")
    await callback.answer()
