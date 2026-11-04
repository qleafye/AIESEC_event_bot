"""Одно личное сообщение сводки и пачка на чужой вход. В группу бот не пишет."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import city_chat_db
from services.chat_tracking import bound_chats
from services.city_chat_gate import outsider_rows
from services.city_chat_link import recipients_for_city
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

PAGE = 25
_WINDOW = timedelta(minutes=2)
_STAMP = "%Y-%m-%d %H:%M:%S"
_locks: dict[int, asyncio.Lock] = {}


def _lock(chat_id: int) -> asyncio.Lock:
    key = int(chat_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def _binding(chat_id: int) -> dict | None:
    wanted = int(chat_id)
    for entry in await bound_chats():
        if entry["chat_id"] == wanted:
            return entry
    return None


def _fresh(opened_at, now: datetime) -> bool:
    try:
        opened = datetime.strptime(opened_at, _STAMP)
    except (TypeError, ValueError):
        return False
    return now - opened < _WINDOW


def _page(rows: list[dict], page: int) -> tuple[list[dict], int]:
    total = len(rows)
    pages = max(1, (total + PAGE - 1) // PAGE) if total else 1
    current = min(max(int(page), 0), pages - 1)
    return rows[current * PAGE:(current + 1) * PAGE], current


def summary_text(title: str, rows: list[dict]) -> str:
    if not rows:
        return "В чате нет людей, которых надо разбирать."
    label = html.escape((title or "").strip() or "чат города")
    lines = [f"В чате «{label}» есть люди, которых надо разобрать."]
    for row in rows:
        name = html.escape(row["name"])
        nick = row.get("username")
        who = f"{name} (@{html.escape(nick)})" if nick else name
        lines.append(f"• {who} — {html.escape(row['reason'])}")
    return "\n".join(lines)


def summary_keyboard(chat_id, rows, page) -> InlineKeyboardMarkup:
    shown, current = _page(rows, page)
    buttons = []
    for row in shown:
        tid = row["telegram_id"]
        buttons.append([
            InlineKeyboardButton(
                text="Оставить", callback_data=f"cchat:keep:{chat_id}:{tid}",
            ),
            InlineKeyboardButton(
                text="Убрать", callback_data=f"cchat:ask:{chat_id}:{tid}",
            ),
        ])
    nav = []
    if current > 0:
        nav.append(InlineKeyboardButton(
            text="Предыдущие", callback_data=f"cchat:page:{chat_id}:{current - 1}",
        ))
    if (current + 1) * PAGE < len(rows):
        nav.append(InlineKeyboardButton(
            text="Ещё", callback_data=f"cchat:page:{chat_id}:{current + 1}",
        ))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_summary(bot, chat_id, admin_id, *, page: int = 0) -> None:
    """Личное сообщение этому админу или правка его пачки, если ей меньше двух минут."""
    entry = await _binding(chat_id)
    if entry is None:
        return
    if int(admin_id) not in await recipients_for_city(entry["city"]):
        return
    rows = await outsider_rows(int(chat_id))
    shown, current = _page(rows, page)
    text = summary_text(entry.get("title") or "", shown if rows else [])
    markup = summary_keyboard(chat_id, rows, current)
    notice = await city_chat_db.summary_notice(int(chat_id), int(admin_id))
    if notice and _fresh(notice.get("opened_at"), msk_now()):
        try:
            await bot.edit_message_text(
                text,
                chat_id=int(admin_id),
                message_id=notice["message_id"],
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise
        return
    sent = await bot.send_message(
        int(admin_id), text, parse_mode="HTML", reply_markup=markup,
    )
    await city_chat_db.save_summary_notice(int(chat_id), int(admin_id), sent.message_id)


async def note_join(bot, chat_id, telegram_id) -> None:
    """Чужой вход открывает или правит пачку. Свой и человек из белого списка её не трогают."""
    if await _binding(chat_id) is None:
        return
    wanted = int(telegram_id)
    rows = await outsider_rows(int(chat_id))
    if not any(row["telegram_id"] == wanted for row in rows):
        return
    async with _lock(chat_id):
        entry = await _binding(chat_id)
        if entry is None:
            return
        rows = await outsider_rows(int(chat_id))
        if not any(row["telegram_id"] == wanted for row in rows):
            return
        for admin_id in await recipients_for_city(entry["city"]):
            try:
                await send_summary(bot, chat_id, admin_id)
            except Exception as exc:
                logger.warning(
                    "city_chat_summary: сводку не отправил, чат %s: %s",
                    chat_id, type(exc).__name__,
                )
