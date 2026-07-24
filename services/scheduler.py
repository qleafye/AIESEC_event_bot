"""Phase 3: persistent AsyncIOScheduler (SCHED-01) + interval jobs (SCHED-03, VERIF refresh).

Tech-stack lock (CLAUDE.md): APScheduler 3.x `AsyncIOScheduler` + `SQLAlchemyJobStore`
on a SEPARATE sqlite file (data/jobs.sqlite), never forum.db (Pitfall 2). Do NOT use the
APScheduler-4.0 API, the in-memory job store, or the thread-based (background) scheduler
— see CLAUDE.md "What NOT to Use".

Job targets are module-level coroutines taking only picklable primitives (an int id /
no args) — never a Bot/closure (Pitfall 3). The Bot is injected once via a module global.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from aiogram.exceptions import TelegramRetryAfter

from config import config
from database.db import get_setting
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_JOBSTORE_URL = "sqlite:///data/jobs.sqlite"

# Injected once at startup. Job coroutines read these module globals — never receive
# a Bot as an arg (keeps persisted job args picklable, Pitfall 3).
_scheduler: AsyncIOScheduler | None = None
_bot = None


# ── Pure helpers (no async, no DB — the unit-test surface) ────────────────────

def _int_or_default(raw, default: int) -> int:
    """Positive int or default. None/empty/garbage/<=0 -> default."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _parse_schedule_dt(raw):
    """Parse admin datetime 'ДД.ММ.ГГГГ ЧЧ:ММ' -> datetime; None on bad input.

    # REG-02: sweep_payment_overdue no longer calls this directly for payment_deadline
    # (reads via get_setting_typed instead) — retained as the parse oracle for
    # tests/test_settings_consumers_phase6.py and tests/test_settings_groups_c0x.py.
    """
    try:
        return datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M")
    except (TypeError, ValueError, AttributeError):
        return None


def _fmt_dt(dt: datetime) -> str:
    """ISO storage format matching db.py ('%Y-%m-%d %H:%M:%S'), lexicographic-safe."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _nudge_cutoff(now: datetime, minutes: int) -> str:
    """now minus `minutes`, ISO-formatted — the started_at threshold for the scan."""
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _nudge_enabled(raw) -> bool:
    """on/None -> True, off -> False (default on)."""
    return raw != "off"


# ── Scheduler lifecycle ──────────────────────────────────────────────────────

def get_scheduler() -> AsyncIOScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialised — call init_scheduler(bot) first")
    return _scheduler


async def init_scheduler(bot):
    """Build the AsyncIOScheduler with a persistent jobstore, register the interval
    jobs, and start it. Date jobs (scheduled broadcasts) auto-restore from the jobstore
    on boot. Returns the running scheduler."""
    global _scheduler, _bot
    _bot = bot
    os.makedirs("data", exist_ok=True)  # jobstore file lives here; SQLAlchemy won't mkdir

    _scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=_JOBSTORE_URL)},
        # ME-01: pin the scheduler timezone to Europe/Moscow. Admin-entered times ("14:30") are
        # parsed into NAIVE datetimes and both scheduled targets (broadcast run_date, payment
        # reminders derived from payment_deadline) are absolute wall-clock times the admin means
        # in Moscow time — never now()+offset. Without this pin APScheduler localizes naive
        # run_dates to the container's tzlocal, so a UTC container fires a "14:30" broadcast 3h
        # off the intended Moscow wall-clock. Pinning makes the naive time fire at 14:30 MSK.
        timezone="Europe/Moscow",
        # WR-04: 1h grace silently DROPPED any date job (scheduled broadcast / payment
        # reminder) whose run_date passed during >1h of downtime — the job never fired, the
        # broadcast row stayed 'pending' forever, no alert. 24h covers realistic deploy/crash
        # windows; send_payment_reminder self-guards on paid/receipt_sent so a late fire is safe.
        job_defaults={"misfire_grace_time": 86400, "coalesce": True},
    )

    # ── interval jobs (registered fresh each boot; replace_existing avoids dupes) ──
    scan_minutes = _int_or_default(await get_setting("nudge_scan_minutes"), 15)
    _scheduler.add_job(
        nudge_incomplete_registrations, "interval",
        minutes=scan_minutes, id="nudge_scan", replace_existing=True,
    )

    refresh_minutes = _int_or_default(await get_setting("allowlist_refresh_minutes"), 60)
    _scheduler.add_job(
        allowlist_refresh_job, "interval",
        minutes=refresh_minutes, id="allowlist_refresh", replace_existing=True,
    )

    # PAY-06: daily overdue sweep (no-op until a payment_deadline is set and passes).
    _scheduler.add_job(
        sweep_payment_overdue, "interval",
        hours=24, id="payment_overdue_sweep", replace_existing=True,
    )

    # Auto-refresh the «Незавершённые» sheet tab so managers don't have to tap the admin
    # button. Interval in hours (setting incomplete_sync_hours, default 2) — light load.
    sync_hours = _int_or_default(await get_setting("incomplete_sync_hours"), 2)
    _scheduler.add_job(
        sync_incomplete_sheet_job, "interval",
        hours=sync_hours, id="incomplete_sheet_sync", replace_existing=True,
    )

    _scheduler.start()
    # ME-03: re-arm any pending broadcast whose date job was dropped from the jobstore during a
    # downtime longer than misfire_grace — otherwise it stays 'pending' forever and never fires.
    await reconcile_scheduled_broadcasts()
    logger.info(
        f"Scheduler started (nudge scan every {scan_minutes}m, "
        f"allowlist refresh every {refresh_minutes}m)"
    )
    return _scheduler


async def reconcile_scheduled_broadcasts():
    """ME-03: on boot, re-schedule any 'pending' scheduled broadcast whose APScheduler date job
    is missing from the jobstore (dropped because its run_date passed during a downtime longer
    than misfire_grace_time). Re-adding with the stored past run_date lets APScheduler fire it
    within the 24h grace; genuinely stale rows (>24h past) still get re-armed and fire on the
    next tick, converting a silently-lost broadcast into a late one. Fail-soft: never blocks
    startup. A job that is still present in the store is left untouched (no double-fire)."""
    try:
        from database.db import list_pending_broadcasts
        pending = await list_pending_broadcasts()
        sched = get_scheduler()
        recovered = 0
        for row in pending:
            bid = row["id"]
            if sched.get_job(f"bcast_{bid}") is not None:
                continue  # live job already scheduled — leave it
            try:
                run_at = datetime.strptime(row["scheduled_at"].strip(), "%Y-%m-%d %H:%M:%S")
            except (KeyError, TypeError, ValueError, AttributeError):
                logger.warning(f"reconcile: broadcast {bid} has unparseable scheduled_at — skipped")
                continue
            schedule_broadcast_job(bid, run_at)
            recovered += 1
        if recovered:
            logger.warning(f"Reconciled {recovered} pending broadcast(s) with dropped jobs")
    except Exception as e:
        logger.error(f"reconcile_scheduled_broadcasts failed: {e}")


# ── SCHED-01: scheduled-broadcast date job ───────────────────────────────────

async def _safe_send(send_coro_factory, chat_id) -> bool:
    """Run one send with the 429-safe single-retry pattern (D-07/D-08).
    Returns True if delivered (first try or retry), False on genuine failure."""
    try:
        await send_coro_factory(chat_id)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await send_coro_factory(chat_id)
            return True
        except Exception as e2:
            logger.error(f"Scheduled send retry failed for {chat_id}: {e2}")
            return False
    except Exception as e:
        logger.error(f"Scheduled send failed for {chat_id}: {e}")
        return False


async def send_scheduled_broadcast(broadcast_id: int):
    """Date-job target: read the payload row by id, resolve the audience, send, mark sent.
    Arg is the int id ONLY (picklable) — the Bot comes from the module global."""
    try:
        from database.db import (
            get_scheduled_broadcast, mark_broadcast_sending, mark_broadcast_sent,
            get_all_users_ids, count_and_list_filtered,
        )
        row = await get_scheduled_broadcast(broadcast_id)
        if not row or row.get("status") != "pending":
            return
        # ME-02: atomically claim (pending → sending) BEFORE the send loop. If this returns 0
        # another fire already claimed it (double-schedule race), so bail. A crash mid-send
        # leaves it 'sending' — never re-fired to the whole audience (the reconciliation only
        # re-arms 'pending' rows). The unsent tail is forfeited by design; admin re-sends.
        if not await mark_broadcast_sending(broadcast_id):
            return

        filter_spec = row.get("filter_spec")
        if filter_spec:
            try:
                target_ids = await count_and_list_filtered(json.loads(filter_spec))
            except Exception as e:
                logger.error(f"Scheduled broadcast {broadcast_id} bad filter_spec: {e}")
                target_ids = []
        else:
            target_ids = await get_all_users_ids()

        text = row.get("text")
        photo = row.get("photo_file_id")
        for chat_id in target_ids:
            if photo:
                await _safe_send(
                    lambda cid: _bot.send_photo(cid, photo, caption=text), chat_id
                )
            else:
                await _safe_send(lambda cid: _bot.send_message(cid, text), chat_id)
            await asyncio.sleep(0.05)

        await mark_broadcast_sent(broadcast_id)
        logger.info(f"Scheduled broadcast {broadcast_id} sent to {len(target_ids)} users")
    except Exception as e:
        logger.error(f"send_scheduled_broadcast({broadcast_id}) failed: {e}")


def schedule_broadcast_job(broadcast_id: int, run_at: datetime):
    get_scheduler().add_job(
        send_scheduled_broadcast, "date",
        run_date=run_at, args=[broadcast_id],
        id=f"bcast_{broadcast_id}", replace_existing=True,
    )


def cancel_broadcast_job(broadcast_id: int):
    try:
        get_scheduler().remove_job(f"bcast_{broadcast_id}")
    except Exception as e:
        logger.warning(f"cancel_broadcast_job({broadcast_id}): {e}")


# ── PAY-06: payment-deadline reminders (D-13) ────────────────────────────────

def schedule_payment_reminder(user_id: int, run_at: datetime, label: str):
    """One-shot deadline reminder for one user. label: 'minus3d' | 'minus1d'
    (disambiguates the job id). replace_existing=True so re-uploading a receipt
    re-schedules cleanly."""
    get_scheduler().add_job(
        send_payment_reminder, "date",
        run_date=run_at, args=[user_id],
        id=f"pay_reminder_{user_id}_{label}", replace_existing=True,
    )


def cancel_payment_reminders(user_id: int):
    """Cancel both outstanding reminders for a user (called on receipt confirm)."""
    for label in ("minus3d", "minus1d"):
        try:
            get_scheduler().remove_job(f"pay_reminder_{user_id}_{label}")
        except Exception:
            pass  # already fired or never scheduled — both fine


async def send_payment_reminder(user_id: int):
    """Date-job target: nudge a non-payer. Arg is int only (picklable); Bot from _bot
    module global. SC#5: never fire if already paid or a receipt is already in review."""
    try:
        from database.db import get_user
        # Gated at FIRE time (not scheduling) so the admin toggle takes effect live — even
        # on reminders already sitting in the jobstore. Default on = prior behaviour.
        if (await get_setting("payment_reminders_enabled") or "on") != "on":
            return
        user = await get_user(user_id)
        if not user or user.get("payment_status") in ("paid", "receipt_sent", None):
            return
        text = await get_setting("payment_reminder_text") or (
            "⏰ Напоминание об оплате участия!\n\n"
            "Срок оплаты истекает скоро. Загрузи чек оплаты через бота."
        )
        await _safe_send(lambda cid: _bot.send_message(cid, text), user_id)
    except Exception as e:
        logger.error(f"send_payment_reminder({user_id}) failed: {e}")


async def sweep_payment_overdue():
    """Daily interval target: mark past-deadline non-payers as 'overdue'. Touches only
    'not_paid' rows that actually entered the payment flow ('payment_option' set or
    'payment_due' populated) — 'receipt_sent'/'paid' are left alone, and the ~590 legacy
    users backfilled to 'not_paid' (who never picked an option) are NOT swept. No-op until
    a parseable payment_deadline is set and has passed."""
    try:
        import aiosqlite
        # REG-02: read through the registry accessor — byte-identical to the previous
        # get_setting + _parse_schedule_dt pair (see tests/test_settings_consumers_phase6.py).
        deadline = await get_setting_typed("payment_deadline")
        if not deadline:
            return
        if datetime.now() < deadline:
            return
        select_where = (
            "payment_status='not_paid' "
            "AND (payment_option IS NOT NULL OR payment_due IS NOT NULL)"
        )
        async with aiosqlite.connect(config.DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT telegram_id FROM users WHERE {select_where}"
            )
            overdue_ids = [row[0] for row in await cursor.fetchall()]
            await db.execute(
                f"UPDATE users SET payment_status='overdue' WHERE {select_where}"
            )
            await db.commit()
        # One final ping to each user we just flipped to 'overdue'. The status change
        # means the next sweep won't re-select them, so this fires exactly once. The
        # status flip above always runs (feeds the «неоплатившие» broadcast segment); only
        # the ping respects the auto-reminders toggle.
        if overdue_ids and (await get_setting("payment_reminders_enabled") or "on") == "on":
            text = await get_setting("payment_overdue_text") or (
                "⚠️ Срок оплаты участия истёк.\n\n"
                "Если ты ещё планируешь участвовать — загрузи чек через бота "
                "(кнопка «💳 Оплата» в меню) или свяжись с организатором."
            )
            for tid in overdue_ids:
                await _safe_send(lambda cid: _bot.send_message(cid, text), tid)
                await asyncio.sleep(0.05)
    except Exception as e:
        logger.error(f"sweep_payment_overdue failed: {e}")


# ── SCHED-03: dropout-nudge interval job ─────────────────────────────────────

DEFAULT_NUDGE_TEXT = (
    "👋 Вы начали регистрацию, но не завершили её. "
    "Отправьте /start, чтобы продолжить — это займёт пару минут."
)


async def sync_incomplete_sheet_job():
    """Interval-job target (no args, picklable). Full-refresh the «Незавершённые» sheet tab
    with the current dropout list every incomplete_sync_hours. Fail-soft."""
    try:
        from database.db import get_incomplete_rows
        from services.sheets import sync_named_worksheet
        from handlers.registration import dropout_step_label
        rows = await get_incomplete_rows()
        # WR-01: get_incomplete_rows returns 4-tuples (tid, username, started_at, last_step).
        # Match the admin-triggered export: 4 headers + human-readable step label (not the raw
        # internal step_key). Without this the 2h auto-sync overwrote the labeled manual export
        # with a narrower, unlabeled sheet each cycle.
        headers = ["ID Telegram", "Username", "Начал регистрацию", "Остановился на"]
        sheet_rows = [
            (tid, uname, started, dropout_step_label(step))
            for tid, uname, started, step in rows
        ]
        await sync_named_worksheet("Незавершённые", headers, sheet_rows)
    except Exception as e:
        logger.error(f"sync_incomplete_sheet_job failed: {e}")


async def nudge_incomplete_registrations():
    """Interval-job target (no args, picklable). Nudge each incomplete registration
    older than the threshold exactly once, then stamp nudged_at (D-14)."""
    try:
        from database.db import get_nudge_candidates, mark_nudged
        if not _nudge_enabled(await get_setting("nudge_enabled")):
            return
        after_minutes = _int_or_default(await get_setting("nudge_after_minutes"), 120)
        cutoff = _nudge_cutoff(datetime.now(), after_minutes)
        candidates = await get_nudge_candidates(cutoff)
        if not candidates:
            return
        text = await get_setting("nudge_text") or DEFAULT_NUDGE_TEXT
        for tid in candidates:
            ok = await _safe_send(lambda cid: _bot.send_message(cid, text), tid)
            if ok:
                await mark_nudged(tid)  # one-shot only after a successful send
            await asyncio.sleep(0.05)
    except Exception as e:
        logger.error(f"nudge_incomplete_registrations failed: {e}")


# ── VERIF: mandatory allowlist-refresh interval job (D-11) ───────────────────

async def allowlist_refresh_job():
    """Interval-job target: reload the RAM allowlist; if it lands empty WHILE gating is
    ON, fire a loud admin alert (fail-open posture, owner-confirmed Open Q2)."""
    try:
        from services.allowlist import refresh_allowlist, allowlist_size
        await refresh_allowlist()
        if allowlist_size() == 0 and (await get_setting("preselect_enabled") or "off") == "on":
            for admin_id in config.ADMIN_IDS:
                try:
                    await _bot.send_message(
                        admin_id,
                        "⚠️ Allowlist пуст — предотбор работает в режиме FAIL-OPEN, "
                        "впускаются ВСЕ. Проверьте Google-таблицу.",
                    )
                except Exception as e:
                    logger.error(f"Allowlist empty-alert to {admin_id} failed: {e}")
    except Exception as e:
        logger.error(f"allowlist_refresh_job failed: {e}")
