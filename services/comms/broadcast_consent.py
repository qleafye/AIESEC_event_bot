"""Кнопка согласия на исходящей рассылке. Без роутера.

Текст письма менеджер пишет сам. Ключа нет в текущем списке — рассылка без кнопки.
Хендлер реэкспортирует те же функции и не импортируется отсюда: у него на уровне
модуля есть handlers.admin.router.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.regform.engine import broadcast_consent_entries


async def consent_button(broadcast_id, consent_key) -> InlineKeyboardMarkup | None:
    """Один ряд с видимым названием. Ключа нет в текущем списке — None, рассылка без кнопки."""
    key = (consent_key or "").strip()
    if not key:
        return None
    entries = await broadcast_consent_entries()
    label = next((lbl for lbl, item in entries if item == key), None)
    if label is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"cns:{int(broadcast_id)}"),
    ]])


async def with_consent_button(base, broadcast_id, consent_key):
    """Кнопка согласия в уже собранную клавиатуру. «🔕» добавляет вызывающий после этого."""
    extra = await consent_button(broadcast_id, consent_key)
    if extra is None:
        return base
    rows = [list(row) for row in (base.inline_keyboard if base else [])]
    rows.extend(list(row) for row in extra.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)
