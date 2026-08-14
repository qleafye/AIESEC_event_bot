"""Phase 2 (APP-08, D-13/D-14): anti-storm periodic pending-application reminder.

A single asyncio background task started at bot startup pings all admins the
pending count once per configurable interval — never one push per submission.
"""
import asyncio
import logging

from aiogram.exceptions import TelegramForbiddenError

from config import config
from database.db import get_pending_count, get_setting
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1800  # seconds (30 min)

# Admins who blocked the bot. The reminder fires on a fixed interval forever, so an admin
# who blocks it produces one identical ERROR per tick for as long as the bot runs (observed
# in production: 67 for a single admin id). Blocking is permanent until the human unblocks,
# and unblocking restarts nothing on our side — so we note it once, stop trying, and let a
# bot restart clear the set (the cheap way to re-test whether they unblocked).
_blocked_admins: set[int] = set()


def _reminder_enabled(raw: str | None) -> bool:
    """on/None -> True, off -> False, unknown -> True (default on)."""
    return raw != "off"


def _reminder_interval(raw: str | None) -> int:
    """Positive int seconds; None/empty/invalid/<=0 -> DEFAULT_INTERVAL.

    # REG-02: pending_reminder_loop no longer calls this directly (reads via
    # get_setting_typed instead) — retained as the parse oracle for
    # tests/test_reminders_phase2.py and tests/test_settings_consumers_phase6.py.
    """
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
            # REG-02: read through the registry accessor (byte-identical to
            # _reminder_interval, see tests/test_settings_consumers_phase6.py).
            interval = await get_setting_typed("pending_reminder_interval")
            if _reminder_enabled(await get_setting("pending_reminder_enabled")):
                count = await get_pending_count()
                if count > 0:
                    text = f"📋 Заявок в ожидании: {count}. Открой /admin → Заявки."
                    for admin_id in config.ADMIN_IDS:
                        if admin_id in _blocked_admins:
                            continue
                        try:
                            await bot.send_message(admin_id, text)
                        except TelegramForbiddenError as e:
                            _blocked_admins.add(admin_id)
                            logger.warning(
                                f"Pending reminder: admin {admin_id} blocked the bot — "
                                f"muting reminders for them until restart: {e}"
                            )
                        except Exception as e:
                            logger.error(f"Pending reminder: failed to notify admin {admin_id}: {e}")
        except Exception as e:
            logger.error(f"Pending reminder loop iteration failed: {e}")
        await asyncio.sleep(interval)
