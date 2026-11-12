"""Кнопка «Выбрать время» и флаг отложенной рассылки. Без роутера.

Текст письма кнопка не пишет. Выключено — клавиатура как была.
Хендлер реэкспортирует те же функции.
"""
from __future__ import annotations

import json

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_BUTTON = "🗓 Выбрать время"


def with_interview_button(base: InlineKeyboardMarkup | None, on: bool):
    """Кнопка «🗓 Выбрать время» первой строкой. Выключено — клавиатура как была."""
    if not on:
        return base
    rows = [[InlineKeyboardButton(text=_BUTTON, callback_data="iv_open")]]
    if base is not None:
        rows.extend(list(row) for row in base.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pack_filter_spec(filters, on: bool) -> str | None:
    """Без галочки — прежний JSON списка. С галочкой — объект, старый список внутри."""
    if not on:
        return json.dumps(filters, ensure_ascii=False) if filters else None
    return json.dumps(
        {"filters": list(filters or []), "interview_button": True}, ensure_ascii=False,
    )


def peel_interview_flag(spec):
    """(галочка, список фильтров или None). Старый список фильтров галочки не имеет."""
    if isinstance(spec, dict):
        filters = spec.get("filters") or None
        return bool(spec.get("interview_button")), filters
    return False, spec
