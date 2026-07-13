import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import config
from database.db import init_db
from handlers import registration, user_actions, admin, payment
from services.reminders import pending_reminder_loop
from services.scheduler import init_scheduler
from services.allowlist import refresh_allowlist
from services.sheets import ensure_sheet_header
from handlers.registration import active_sheet_headers, set_sheet_schema
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

def _configure_logging():
    """Full detail → rotating file on a mounted volume (survives container recreate,
    greppable on the host); only WARNING+ → stdout so `docker logs` stays clean.
    File level is set by LOG_LEVEL (.env); flip to DEBUG when digging."""
    os.makedirs("logs", exist_ok=True)
    level = getattr(logging, (config.LOG_LEVEL or "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        "logs/bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.WARNING)  # docker logs = problems only
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    # Tame chatty third-party loggers so the file isn't drowned in framework noise.
    for noisy in ("aiogram.event", "apscheduler", "urllib3", "gspread", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main():
    _configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting bot...")
    
    # Init DB
    await init_db()

    # Ensure the Google Sheet has a column-name header row (fail-soft, off-thread).
    # Only the enabled-question columns — set the event type/preset before delegates register.
    _hdrs = await active_sheet_headers()
    # CR-9: freeze the header snapshot alongside the physical header write so appended rows
    # stay aligned even if a question is toggled mid-event. Fail-soft — never blocks startup.
    try:
        await set_sheet_schema(_hdrs)
    except Exception:
        logger.warning("Failed to snapshot sheet schema at startup", exc_info=True)
    asyncio.create_task(ensure_sheet_header(_hdrs))
    
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    
    session = None
    if config.PROXY_URL:
        session = AiohttpSession(proxy=config.PROXY_URL.get_secret_value())
        logger.info("Using configured proxy")

    bot = Bot(token=config.BOT_TOKEN.get_secret_value(), default=default, session=session)
    dp = Dispatcher(storage=MemoryStorage())

    # CR-8: global error handler. Without this, any unhandled exception in a handler is
    # silently dropped (the update just vanishes). Log the exception WITH the offending
    # update so failures are visible, and fail soft (return True = handled).
    @dp.errors()
    async def _on_update_error(event: ErrorEvent):
        logger.error("Unhandled update error: %s", event.exception, exc_info=event.exception)
        try:
            logger.error("Failing update: %s", event.update.model_dump_json(exclude_none=True))
        except Exception:
            pass
        return True

    # Register routers
    payment.init_payment_module(dp.storage)  # out-of-handler FSMContext for free/single-option path
    dp.include_router(admin.router) # Admin first to intercept commands
    dp.include_router(payment.router)  # payment callbacks/states checked before registration
    dp.include_router(registration.router)
    dp.include_router(user_actions.router)
    
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(pending_reminder_loop(bot))
    logger.info("Pending-application reminder task started")

    # Phase 3: persistent scheduler (SCHED-01/03) + warm the pre-selection allowlist (VERIF)
    await init_scheduler(bot)
    asyncio.create_task(refresh_allowlist())
    logger.info("Scheduler + allowlist refresh started")

    await dp.start_polling(bot)
    logger.info("Bot started polling")

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped!")
