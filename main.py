import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import config
from database.db import init_db, get_setting, set_setting
from handlers import registration, user_actions, admin, payment, polls
from services.reminders import pending_reminder_loop
from services.scheduler import init_scheduler
from services.allowlist import warm_allowlist_if_gating_on
from services.sheets import ensure_sheet_header
from services.background import spawn as _spawn, cancel_all as cancel_background_tasks
import services.miniapp_outbox as miniapp_outbox
from services.heartbeat import PollingHeartbeatMiddleware, heartbeat_loop, clear_heartbeat
import services.sheets as sheets_service
import services.proxy_session as proxy_session
from services.proxy_session import FailoverAiohttpSession, build_proxy_chain, mask_proxy_url
from handlers.registration import active_sheet_headers, set_sheet_schema, party_sheet_headers, PARTY_SHEET_TAB_DEFAULT, short_sheet_headers, SHORT_SHEET_TAB_DEFAULT, get_sheet_schema, city_row_tab
from cities import enabled_cities, is_default_city, seed_cities_if_empty, reload_cities
from settings_schema import get_setting_typed, SETTINGS_SCHEMA
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramBadRequest

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


async def _maybe_ensure_short_sheet_header():
    """Phase 7 (SHORT-02, plan 07-02): create/sync the short tab's header ONLY when
    registration_mode is 'short' — same posture as _maybe_ensure_party_sheet_header (D-11):
    a manager who never flips the promo toggle on must never see an extra tab materialize in
    the spreadsheet, even if a __short question toggle gets flipped in the meantime. Fail-soft:
    Sheets being unreachable must never block startup."""
    if await get_setting_typed("registration_mode") != "short":
        return
    tab = await get_setting("short_sheet_tab") or SHORT_SHEET_TAB_DEFAULT
    try:
        headers = await short_sheet_headers()
        await sheets_service.ensure_named_sheet_header(tab, headers)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to ensure short sheet header (tab={tab!r}): {e}")


async def _maybe_ensure_city_sheet_headers():
    """Phase 07.1 (CITY-02, plan 07.1-02): materialize each ENABLED non-default city's tabs at
    startup — same D-11/D-15 posture as the two siblings above ("new capability defaults OFF
    must never appear in the spreadsheet"). Gated on the master `event_city_enabled` toggle
    first; Moscow is skipped entirely because its tabs are already handled by the three
    existing calls in main() (byte-identical legacy tab names). Each tab is its own
    try/except so one city's Sheets failure never cancels the rest, and no exception here can
    ever block bot startup/polling."""
    if await get_setting_typed("event_city_enabled") != "on":
        return
    logger = logging.getLogger(__name__)
    for city in await enabled_cities():
        code = city["code"]
        if is_default_city(code):
            continue
        base = city.get("tab_base") or ""
        if not base:
            continue

        # Main tab — always materialized for an enabled non-default city.
        try:
            tab = await city_row_tab(code, None)
            headers = await get_sheet_schema()  # CR-9 frozen snapshot, not live headers
            await sheets_service.ensure_named_sheet_header(tab, headers)
        except Exception as e:
            logger.warning(f"Failed to ensure city sheet header (tab={tab!r}): {e}")

        # Party tab — only while the party track itself is enabled.
        if await get_setting_typed("party_enabled") == "on":
            try:
                tab = await city_row_tab(code, "party_overnight")
                headers = await party_sheet_headers()
                await sheets_service.ensure_named_sheet_header(tab, headers)
            except Exception as e:
                logger.warning(f"Failed to ensure city sheet header (tab={tab!r}): {e}")

        # Short tab — only while the short/promo track itself is enabled.
        if await get_setting_typed("registration_mode") == "short":
            try:
                tab = await city_row_tab(code, "short")
                headers = await short_sheet_headers()
                await sheets_service.ensure_named_sheet_header(tab, headers)
            except Exception as e:
                logger.warning(f"Failed to ensure city sheet header (tab={tab!r}): {e}")

        # «{base} Незавершённые» is deliberately NOT created here — plan 07.1-04 writes it via
        # a full sync_named_worksheet rewrite, created there on-demand once rows exist.


async def seed_main_sheet_tab_from_env() -> bool:
    """Phase 14 (CFG-01): GOOGLE_SHEET_TAB (.env) is DEPRECATED as a day-to-day setting — it is
    read exactly ONCE at startup to seed bot_settings.main_sheet_tab, then ignored forever.
    Idempotent by design (T-14-06): if a manager has already picked a tab from «⚙️ Настройки →
    📄 Вкладки таблицы», this function must NOT overwrite it — otherwise every container
    restart would silently revert the manager's own choice back to whatever .env still says.
    Must run AFTER init_db() and BEFORE the first active_sheet_headers() resolve (services/sheets.py
    stage 2 still reads config.GOOGLE_SHEET_TAB as a safety-net fallback — leave that chain
    intact, this is only the one-time migration write)."""
    value = (config.GOOGLE_SHEET_TAB or "").strip().strip('"').strip("'").strip()
    if not value:
        return False
    existing = (await get_setting("main_sheet_tab")) or ""
    if existing.strip():
        return False
    await set_setting("main_sheet_tab", value)
    logging.getLogger(__name__).warning(
        "GOOGLE_SHEET_TAB в .env устарел (Phase 14, CFG-01): значение %r один раз перенесено в "
        "bot_settings.main_sheet_tab. Дальше меняйте вкладку из «⚙️ Настройки → 📄 Вкладки "
        "таблицы» в боте — .env для этого больше не читается.",
        value,
    )
    return True


async def seed_proxy_settings_from_env() -> None:
    """Phase 14 (CFG-01): PROXY_RECHECK_SECONDS/PROXY_CONNECT_TIMEOUT (.env) move to the
    registry (group «🔧 Система») so a manager can change them without a redeploy — but
    FailoverAiohttpSession still reads both values only once, at process start (see
    services/proxy_session.py), so a registry edit needs a restart to take effect (honestly
    stated in both settings' prompts). Same one-time, non-destructive seed idiom as
    seed_main_sheet_tab_from_env: only writes when bot_settings has no row yet AND the .env
    value differs from the registry default — never overwrites a manager's own edit, and a
    server whose .env still matches the shipped default writes nothing at all."""
    logger = logging.getLogger(__name__)
    pairs = (
        ("proxy_recheck_seconds", config.PROXY_RECHECK_SECONDS),
        ("proxy_connect_timeout", config.PROXY_CONNECT_TIMEOUT),
    )
    for key, env_value in pairs:
        existing = await get_setting(key)
        if (existing or "").strip():
            continue
        if env_value == SETTINGS_SCHEMA[key]["default"]:
            continue
        await set_setting(key, str(env_value))
        logger.warning(
            "PROXY_* в .env устарел (Phase 14, CFG-01): %s=%r один раз перенесено в "
            "bot_settings.%s. Дальше меняйте из «⚙️ Настройки → 🔧 Система» в боте (нужен "
            "перезапуск, чтобы значение применилось).",
            key, env_value, key,
        )

# Two Telegram-side outcomes are normal user behaviour, not bot faults: double-tapping a
# button re-renders an identical screen ("message is not modified"), and answering a
# callback after Telegram's ~15-minute window ("query is too old"). Both used to be logged
# at ERROR with the whole update body, which is what inflated the production log — they are
# demoted to DEBUG so ERROR keeps meaning "something actually broke".
_BENIGN_TELEGRAM_ERRORS = ("message is not modified", "query is too old")

_UPDATE_EVENT_ATTRS = (
    "message", "edited_message", "callback_query", "inline_query", "my_chat_member",
    "chat_member", "chat_join_request", "pre_checkout_query", "channel_post",
)


def _update_identity(update) -> dict:
    """Pick out the *identifiers* of a failing update — never its content.

    The log file lives on a host volume (`./logs:/app/logs`, rotated but persistent) and a
    registration bot's updates carry PII: full name, phone, e-mail, questionnaire answers,
    resume/receipt file_ids, free-text messages. Logging `update.model_dump_json()` put all
    of that on disk for every unhandled error. What is actually needed to debug is the update
    id, who sent it and where — plus the exception itself (logged with traceback).
    """
    ident = {"update_id": getattr(update, "update_id", None), "event": None}
    user = chat = None
    for attr in _UPDATE_EVENT_ATTRS:
        obj = getattr(update, attr, None)
        if obj is None:
            continue
        ident["event"] = attr
        user = getattr(obj, "from_user", None)
        chat = getattr(obj, "chat", None)
        if chat is None:  # callback_query carries the chat via its .message
            chat = getattr(getattr(obj, "message", None), "chat", None)
        break
    ident["telegram_user_id"] = getattr(user, "id", None)
    ident["chat_id"] = getattr(chat, "id", None)
    return ident


async def on_update_error(event: ErrorEvent) -> bool:
    """Global aiogram error handler (registered via ``dp.errors()`` in ``main``).

    Logs the exception class with traceback and only the *identity* of the failing update
    (update_id / event type / telegram_user_id / chat_id / handler name when aiogram exposes
    it). Never serialises the update body — that is PII on a persistent volume.
    """
    logger = logging.getLogger(__name__)
    message = str(event.exception)
    if isinstance(event.exception, TelegramBadRequest) and any(
        marker in message for marker in _BENIGN_TELEGRAM_ERRORS
    ):
        logger.debug("Benign Telegram update error: %s", message)
        return True
    try:
        ident = _update_identity(event.update)
    except Exception:
        ident = {}
    handler_name = None
    try:  # best effort: aiogram may expose the raising handler on the event's context
        h = getattr(event, "handler", None)
        cb = getattr(h, "callback", None) if h is not None else None
        handler_name = getattr(cb, "__qualname__", None)
    except Exception:
        handler_name = None
    logger.error(
        "Unhandled update error: %s (update_id=%s event=%s telegram_user_id=%s chat_id=%s handler=%s)",
        type(event.exception).__name__,
        ident.get("update_id"),
        ident.get("event"),
        ident.get("telegram_user_id"),
        ident.get("chat_id"),
        handler_name,
        exc_info=event.exception,
    )
    return True


async def main():
    _configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting bot...")
    
    # Init DB
    await init_db()

    # Phase 14 (CITY-07): one-time .env -> `cities` table seed, then load the in-memory cache
    # from the DB. MUST run before active_sheet_headers()/_maybe_ensure_city_sheet_headers()
    # below -- that function already reads cities.enabled_cities(), so the cache must be
    # populated first. `await`, not `_spawn` -- unlike the header-ensure calls, nothing may
    # read the cities cache before it reflects the DB.
    await seed_cities_if_empty()
    await reload_cities()

    # Phase 14 (CFG-01): one-time GOOGLE_SHEET_TAB -> bot_settings.main_sheet_tab migration.
    # MUST run before active_sheet_headers() below — otherwise the very first header resolve
    # would fall through to services/sheets.py stage 2 (reading .env directly) instead of
    # seeing the seeded registry value.
    await seed_main_sheet_tab_from_env()
    await seed_proxy_settings_from_env()

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
    # Phase 7 (SHORT-02): parallel short-tab header call, gated on registration_mode=="short" —
    # see _maybe_ensure_short_sheet_header's docstring. Does not touch the calls/order above.
    _spawn(_maybe_ensure_short_sheet_header())
    # Phase 07.1 (CITY-02): parallel per-city tab header call, gated on event_city_enabled —
    # see _maybe_ensure_city_sheet_headers's docstring. Does not touch the calls/order above.
    _spawn(_maybe_ensure_city_sheet_headers())

    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    
    # Failover proxy chain (2026-08-06 incident: a single dead proxy took the whole bot
    # down). PROXY_URL_BACKUP is optional -- with only PROXY_URL set (or nothing), the
    # chain collapses to length 1 and FailoverAiohttpSession.make_request delegates straight
    # to the base class, byte-identical to the old single-proxy behaviour.
    chain = build_proxy_chain(
        config.PROXY_URL.get_secret_value() if config.PROXY_URL else None,
        config.PROXY_URL_BACKUP.get_secret_value() if config.PROXY_URL_BACKUP else None,
    )
    session = None
    if chain != [None]:
        # Phase 14 (CFG-01): both timings now come from the registry (seeded once from .env
        # above by seed_proxy_settings_from_env) — read here via get_setting_typed, not
        # config.PROXY_*, and fail-soft to the registry default on a None/empty value so a
        # broken bot_settings row can never prevent the bot from starting.
        recheck_seconds = await get_setting_typed("proxy_recheck_seconds")
        if recheck_seconds is None:
            recheck_seconds = SETTINGS_SCHEMA["proxy_recheck_seconds"]["default"]
        connect_timeout = await get_setting_typed("proxy_connect_timeout")
        if connect_timeout is None:
            connect_timeout = SETTINGS_SCHEMA["proxy_connect_timeout"]["default"]
        recheck_seconds = int(recheck_seconds)
        connect_timeout = int(connect_timeout)
        session = FailoverAiohttpSession(
            chain,
            recheck_seconds=recheck_seconds,
            connect_timeout=connect_timeout,
        )
        logger.info(
            "Using proxy chain: %s (connect_timeout=%s)",
            [mask_proxy_url(link) for link in chain],
            connect_timeout,
        )

    bot = Bot(token=config.BOT_TOKEN.get_secret_value(), default=default, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    # Docker HEALTHCHECK: session-middleware отмечает каждый успешный getUpdates, фоновая
    # задача ниже пишет heartbeat-файл только пока эти отметки свежие — т.е. файл отражает
    # живость поллинга, а не event loop. Семантика целиком — в services/heartbeat.py.
    bot.session.middleware(PollingHeartbeatMiddleware())

    # CR-8: global error handler. Without this, any unhandled exception in a handler is
    # silently dropped (the update just vanishes). Fails soft (return True = handled). The
    # body is module-level `on_update_error` so it is unit-testable — it logs identifiers only,
    # never the update payload (PII on a persistent log volume).
    dp.errors()(on_update_error)

    # Register routers
    payment.init_payment_module(dp.storage)  # out-of-handler FSMContext for free/single-option path
    # Quick 260904-3vm (эстафета): MemoryStorage бота никто извне не сбрасывает — это
    # единственный путь веб-процесса (miniapp_outbox::reg_fsm_reset) к FSM бота.
    miniapp_outbox.init_fsm_storage(dp.storage)
    dp.include_router(admin.router) # Admin first to intercept commands
    dp.include_router(payment.router)  # payment callbacks/states checked before registration
    dp.include_router(registration.router)
    dp.include_router(user_actions.router)
    # Опросы: poll_answer/poll — другие типы апдейтов, с message/callback не конкурируют.
    # Без этого роутера aiogram не попросит у Telegram poll_answer/poll (allowed_updates
    # собираются из зарегистрированных observer'ов) — закреплено tests/test_polls_260822.py.
    dp.include_router(polls.router)

    await bot.delete_webhook(drop_pending_updates=True)
    _spawn(pending_reminder_loop(bot))
    logger.info("Pending-application reminder task started")

    # Phase 3: persistent scheduler (SCHED-01/03) + warm the pre-selection allowlist (VERIF)
    await init_scheduler(bot)
    # P0 audit T-dw1-02: inject the bot into sheets.py so an exhausted-retry append can alert
    # admins; must be live before polling starts.
    sheets_service.set_alert_bot(bot)
    # Quick task 260813: warn admins once per start if GOOGLE_SHEET_TAB is empty (main tab
    # would resolve by position on first use) — fail-soft, never blocks startup.
    await sheets_service.warn_if_tab_unconfigured()
    # Same fail-soft injection for proxy_session's failover-switch admin alert -- must also
    # be live before polling starts (the very first proxy rotation could happen immediately).
    proxy_session.set_alert_bot(bot)
    _spawn(warm_allowlist_if_gating_on())
    _spawn(heartbeat_loop())
    logger.info("Scheduler + allowlist refresh + heartbeat started")

    # Phase 19 (08, D-10/T-19-52): chat menu button reflects miniapp_enabled at startup too —
    # without this, a toggle flip made while the bot was down would only take effect on the
    # NEXT admin visit to the settings screen, not on restart. Fail-soft: unreachable Telegram
    # must not block startup.
    try:
        from handlers.admin_miniapp import sync_chat_menu_button
        await sync_chat_menu_button(bot)
    except Exception:
        logger.warning("sync_chat_menu_button failed at startup", exc_info=True)

    try:
        await dp.start_polling(bot)
        logger.info("Bot started polling")
    finally:
        # Heartbeat: поллинг закончился (любой причиной) — контейнер не должен выглядеть
        # здоровым до истечения возраста файла.
        clear_heartbeat()
        # WR-03: graceful shutdown — the SQLAlchemyJobStore holds an open engine to
        # data/jobs.sqlite and the bot session holds connector sockets. Don't rely on
        # interpreter teardown (fragile under SIGTERM restarts / test imports).
        try:
            from services.scheduler import get_scheduler
            get_scheduler().shutdown(wait=False)
        except Exception:
            logger.warning("Scheduler shutdown failed", exc_info=True)
        # Background tasks (reminder loop, allowlist warm-up, sheet drains, album/welcome
        # sends) hold the bot and make Telegram/Sheets calls; cancel and drain them BEFORE
        # the session closes, otherwise they die mid-call on a closed ClientSession.
        try:
            cancelled = await cancel_background_tasks()
            if cancelled:
                logger.info("Cancelled %d background task(s) on shutdown", cancelled)
        except Exception:
            logger.warning("Background task cancellation failed", exc_info=True)
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
