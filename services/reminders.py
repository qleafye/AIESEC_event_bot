"""Phase 2 (APP-08, D-13/D-14): anti-storm periodic pending-application reminder.

A single asyncio background task started at bot startup pings all admins the
pending count once per configurable interval — never one push per submission.
"""
import asyncio
import logging

from config import config
from database.db import get_pending_count, get_setting

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1800  # seconds (30 min)


def _reminder_enabled(raw: str | None) -> bool:
    """on/None -> True, off -> False, unknown -> True (default on)."""
    return raw != "off"


def _reminder_interval(raw: str | None) -> int:
    """Positive int seconds; None/empty/invalid/<=0 -> DEFAULT_INTERVAL."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return value if value > 0 else DEFAULT_INTERVAL


async def pending_reminder_loop(bot):
    """Forever: if enabled and pending count > 0, ping every admin once, then sleep
    the configured interval. Fail-soft per iteration and per admin send."""
    while True:
        interval = DEFAULT_INTERVAL
        try:
            interval = _reminder_interval(await get_setting("pending_reminder_interval"))
            if _reminder_enabled(await get_setting("pending_reminder_enabled")):
                count = await get_pending_count()
                if count > 0:
                    text = f"📋 Заявок в ожидании: {count}. Открой /admin → Заявки."
                    for admin_id in config.ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, text)
                        except Exception as e:
                            logger.error(f"Pending reminder: failed to notify admin {admin_id}: {e}")
        except Exception as e:
            logger.error(f"Pending reminder loop iteration failed: {e}")
        await asyncio.sleep(interval)
