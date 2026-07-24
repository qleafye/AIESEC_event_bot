import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import config
from database.db import init_db, get_setting
from handlers import registration, user_actions, admin, payment
from services.reminders import pending_reminder_loop
from services.scheduler import init_scheduler
from services.allowlist import refresh_allowlist
from services.sheets import ensure_sheet_header
from services.background import spawn as _spawn
import services.sheets as sheets_service
from handlers.registration import active_sheet_headers, set_sheet_schema, party_sheet_headers, PARTY_SHEET_TAB_DEFAULT
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


# WR-02 / audit systemic fix: strong-ref fire-and-forget helper now lives in
# services.background.spawn (imported above as _spawn) so handlers can share it without a
# circular import. Keeps GC from dropping suspended background tasks mid-run.


async def _maybe_ensure_party_sheet_header():
    """Phase 5 (D-11, plan 05-06): create/sync the party tab's header ONLY when party_enabled
    is 'on' — a bot that never turns the party track on must never create the tab, keeping the
    D-15 "new capability defaults OFF" posture visible in the spreadsheet itself. Extracted as
    its own awaitable (rather than inlined) so the gating decision is independently testable
    without a live Sheets call. Fail-soft: Sheets being unreachable must never block startup."""
    if (await get_setting("party_enabled") or "off") != "on":
        return
    tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
    try:
        headers = await party_sheet_headers()
        await sheets_service.ensure_named_sheet_header(tab, headers)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to ensure party sheet header (tab={tab!r}): {e}")


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
    _spawn(ensure_sheet_header(_hdrs))
    # Phase 5 (D-11): parallel party-tab header call, gated on party_enabled — see
    # _maybe_ensure_party_sheet_header's docstring. Does not touch the call/order above.
    _spawn(_maybe_ensure_party_sheet_header())

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
    _spawn(pending_reminder_loop(bot))
    logger.info("Pending-application reminder task started")

    # Phase 3: persistent scheduler (SCHED-01/03) + warm the pre-selection allowlist (VERIF)
    await init_scheduler(bot)
    # P0 audit T-dw1-02: inject the bot into sheets.py so an exhausted-retry append can alert
    # admins; must be live before polling starts.
    sheets_service.set_alert_bot(bot)
    _spawn(refresh_allowlist())
    logger.info("Scheduler + allowlist refresh started")

    try:
        await dp.start_polling(bot)
        logger.info("Bot started polling")
    finally:
        # WR-03: graceful shutdown — the SQLAlchemyJobStore holds an open engine to
        # data/jobs.sqlite and the bot session holds connector sockets. Don't rely on
        # interpreter teardown (fragile under SIGTERM restarts / test imports).
        try:
            from services.scheduler import get_scheduler
            get_scheduler().shutdown(wait=False)
        except Exception:
            logger.warning("Scheduler shutdown failed", exc_info=True)
        try:
            await bot.session.close()
        except Exception:
            logger.warning("Bot session close failed", exc_info=True)

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
             asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped!")
