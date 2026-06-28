import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import config
from database.db import init_db
from handlers import registration, user_actions, admin
from services.reminders import pending_reminder_loop
from services.scheduler import init_scheduler
from services.allowlist import refresh_allowlist
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

async def main():
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info("Starting bot...")
    
    # Init DB
    await init_db()
    
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    
    session = None
    if config.PROXY_URL:
        session = AiohttpSession(proxy=config.PROXY_URL.get_secret_value())
        logger.info("Using configured proxy")

    bot = Bot(token=config.BOT_TOKEN.get_secret_value(), default=default, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    
    # Register routers
    dp.include_router(admin.router) # Admin first to intercept commands
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
