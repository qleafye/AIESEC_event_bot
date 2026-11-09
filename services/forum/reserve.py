"""Резерв = живой автоотказ. Приглашение на день не меняет status."""
from __future__ import annotations

from database import db
from services.infra.timeutil import msk_now


async def is_live_reserve(user: dict | None) -> bool:
    if not user or user.get("status") != "rejected":
        return False
    return await db.get_live_auto_reject_log_entry(int(user["telegram_id"])) is not None


def today_msk() -> str:
    return msk_now().strftime("%Y-%m-%d")


async def invited_today(telegram_id: int, *, city: str | None = None, day: str | None = None) -> bool:
    return await db.is_invited_reserve(telegram_id, day or today_msk(), city=city)


async def may_enter_as_reserve(user: dict | None) -> bool:
    if not await is_live_reserve(user):
        return False
    city = (user or {}).get("event_city")
    return await invited_today(int(user["telegram_id"]), city=city)
