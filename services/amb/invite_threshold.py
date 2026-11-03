"""Строка порога на уже существующей «Моя ссылка».

Считает тех, кто завершил анкету по ссылке: тот же список, что «Мои приглашённые».
Ступени и статус амбассадора не читает и в базу не пишет.
"""
from __future__ import annotations

from database.db import get_referrals
from domain.settings.schema import get_setting_typed


def _fmt(template: str, **subs) -> str:
    try:
        return template.format(**subs)
    except (KeyError, IndexError, ValueError):
        return template


async def threshold_line(user_id: int, *, progress_shown: bool, tr) -> str:
    """Одна строка. `progress_shown` — числа приглашённых на экране уже есть, count не повторять."""
    raw = await get_setting_typed("amb_invite_threshold")
    threshold = raw if isinstance(raw, int) and raw >= 1 else 5
    count = len(await get_referrals(user_id))
    left = max(0, threshold - count)
    if left == 0:
        key = (
            "amb_invite_threshold_done_text" if progress_shown
            else "amb_invite_threshold_done_with_count_text"
        )
    else:
        key = (
            "amb_invite_threshold_left_text" if progress_shown
            else "amb_invite_threshold_with_count_text"
        )
    return _fmt(await tr(key), count=count, threshold=threshold, left=left)
