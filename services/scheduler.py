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
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import config
from database.db import get_setting
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_JOBSTORE_URL = "sqlite:///data/jobs.sqlite"

# TZFIX-260816: the ONE place the timezone literal is named in the whole codebase. Both the
# scheduler pin below (init_scheduler) and _now_moscow_naive() read this constant, so they
# structurally cannot drift apart — that drift (pin=Moscow, checks=container clock/UTC) was
# exactly the bug this fix closes. See .planning/TZFIX-260816.md.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Night review 260816 (review/services.md #2, #4). ONE source for the misfire grace: it feeds
# both `job_defaults` below and the staleness threshold in reconcile_scheduled_broadcasts(), so
# the reconciliation cannot drift away from the grace the executor actually enforces.
_MISFIRE_GRACE_SECONDS = 86400
# How soon after boot an interval job whose saved run time already passed is caught up. Not
# "right now": it keeps the first run from colliding with startup (long polling still coming up).
_BOOT_CATCHUP = timedelta(minutes=2)
# Review 260817 §B2 (п.10): a 'sending' broadcast row older than this is treated as a crashed
# send and reclaimed at boot (reconcile_scheduled_broadcasts → db.reclaim_stale_sending). The
# re-run is idempotent per recipient (scheduled_broadcast_deliveries), so the threshold only
# guards against reclaiming a send that is genuinely still running in a sibling process
# (rolling restart), not against double delivery.
_STALE_SENDING_MINUTES = 10

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


def _interval_matches(job, interval: timedelta) -> bool:
    """True only if `job` is an existing interval job running on exactly `interval`.

    Deliberately duck-typed (getattr, no isinstance against APScheduler classes): the only
    thing that matters is that the persisted trigger fires on the same period, so its saved
    `next_run_time` is still meaningful. Anything else — no job, a date trigger, a period the
    manager has since changed in the settings — is False, and the schedule is recomputed.
    """
    trigger = getattr(job, "trigger", None)
    return bool(getattr(trigger, "interval", None) == interval)


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


def _now_moscow_naive() -> datetime:
    """TZFIX-260816: naive (tzinfo=None) Moscow wall-clock time.

    Naive because it is compared against naive admin input (`_parse_schedule_dt`) and naive
    values pulled from settings — the same shape the scheduler pin in `init_scheduler` (MOSCOW_TZ,
    ~line 98) already expects. The bug this fixes: the container clock (python:3.11-slim, no
    ENV TZ) runs on UTC, while the scheduler is pinned to Europe/Moscow. A bare `datetime.now()`
    reads the container's UTC clock, so in the 3-hour window between UTC and MSK wall-clock, a
    validation that should reject a past broadcast/deadline time instead let it through — and
    `misfire_grace_time=86400` then fired the stale job immediately to the whole audience.

    Do NOT use this helper where the comparison is against a value the bot itself stamped via
    `datetime.now()` (see `_nudge_cutoff` — that one stays on the container clock).
    """
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def _nudge_cutoff(now: datetime, minutes: int) -> str:
    """now minus `minutes`, ISO-formatted — the started_at threshold for the scan.

    TZFIX-260816: `now` here is deliberately the container clock — see the call site comment
    at nudge_incomplete_registrations (scheduler.py:413) for why this must NOT switch to
    _now_moscow_naive().
    """
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _nudge_enabled(raw) -> bool:
    """on/None -> True, off -> False (default on)."""
    return raw != "off"


# ── Scheduler lifecycle ──────────────────────────────────────────────────────

def get_scheduler() -> AsyncIOScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialised — call init_scheduler(bot) first")
    return _scheduler


def _add_interval_job(func, job_id: str, interval: timedelta):
    """Register one interval job, KEEPING the schedule persisted in the jobstore.

    Night review 260816 (review/services.md #2). Why the explicit `next_run_time` is the whole
    point: `add_job` without it lets `_real_add_job` fill in
    `trigger.get_next_fire_time(None, now)` (schedulers/base.py:1068-1071), and an
    `IntervalTrigger` with no `start_date` answers `now + interval`
    (triggers/interval.py:69). With `replace_existing=True` the store is then rewritten via
    `update_job` (schedulers/base.py:1075-1080) — so every boot pushed the next run to
    boot+interval. A bot restarted more often than once a day therefore NEVER ran the 24h
    `sweep_payment_overdue`: nobody was flipped to 'overdue' and the «неоплатившие» segment
    stayed empty.

    Rules:
      * same interval + saved run time still in the future -> reuse it (the fix);
      * same interval + saved run time already passed / missing -> now + `_BOOT_CATCHUP`.
        Passing the past time back would be dropped as a misfire for anything older than
        `misfire_grace_time` (executors/base.py:117-127) — the silent loss we are closing;
      * no job yet, or the manager changed the interval in the settings -> no explicit
        `next_run_time` at all, i.e. byte-for-byte the previous behaviour (boot + new
        interval). A settings change must apply, not be shadowed by an old schedule.

    Requires the scheduler to be started (paused is enough): while it is STATE_STOPPED,
    `get_job()` only looks at `_pending_jobs` and never reads the jobstore
    (schedulers/base.py:1012-1016).
    """
    kwargs = {}
    existing = _scheduler.get_job(job_id)
    if _interval_matches(existing, interval):
        now = datetime.now(MOSCOW_TZ)
        saved = getattr(existing, "next_run_time", None)
        if saved is not None and saved > now:
            kwargs["next_run_time"] = saved
        else:
            kwargs["next_run_time"] = now + _BOOT_CATCHUP
            logger.info(
                f"Job {job_id}: saved run time {saved} already passed (downtime) — "
                f"catching up at {kwargs['next_run_time']}"
            )
    _scheduler.add_job(
        func, "interval", seconds=int(interval.total_seconds()),
        id=job_id, replace_existing=True, **kwargs,
    )


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
        # TZFIX-260816: same MOSCOW_TZ constant now also feeds _now_moscow_naive() below, so this
        # pin and the admin-input validations can no longer read the timezone differently.
        timezone=MOSCOW_TZ,
        # WR-04: 1h grace silently DROPPED any date job (scheduled broadcast / payment
        # reminder) whose run_date passed during >1h of downtime — the job never fired, the
        # broadcast row stayed 'pending' forever, no alert. 24h covers realistic deploy/crash
        # windows; send_payment_reminder self-guards on paid/receipt_sent so a late fire is safe.
        job_defaults={"misfire_grace_time": _MISFIRE_GRACE_SECONDS, "coalesce": True},
    )

    # Night review 260816 (review/services.md #2): started BEFORE the jobs are registered,
    # because while the scheduler is STATE_STOPPED `get_job()` only consults `_pending_jobs`
    # and never opens the jobstore (schedulers/base.py:1012-1016) — the schedule saved by the
    # previous run would be unreadable, and `_add_interval_job` could not preserve it.
    # `paused=True` rather than a plain start(): it brings the jobstore up without waking
    # anything, so there is no window in which an overdue saved job fires a split second
    # before our add_job rewrites it. resume() happens once the whole schedule is assembled.
    _scheduler.start(paused=True)

    # ── interval jobs (registered fresh each boot; replace_existing avoids dupes) ──
    scan_minutes = _int_or_default(await get_setting("nudge_scan_minutes"), 15)
    _add_interval_job(nudge_incomplete_registrations, "nudge_scan", timedelta(minutes=scan_minutes))

    refresh_minutes = _int_or_default(await get_setting("allowlist_refresh_minutes"), 60)
    _add_interval_job(allowlist_refresh_job, "allowlist_refresh", timedelta(minutes=refresh_minutes))

    # PAY-06: daily overdue sweep (no-op until a payment_deadline is set and passes).
    _add_interval_job(sweep_payment_overdue, "payment_overdue_sweep", timedelta(hours=24))

    # Auto-refresh the «Незавершённые» sheet tab so managers don't have to tap the admin
    # button. Interval in hours (setting incomplete_sync_hours, default 2) — light load.
    sync_hours = _int_or_default(await get_setting("incomplete_sync_hours"), 2)
    _add_interval_job(sync_incomplete_sheet_job, "incomplete_sheet_sync", timedelta(hours=sync_hours))

    # Phase 19 (08, D-01/Pattern 7): разбор miniapp_outbox — побочные эффекты записи из
    # Mini App (уведомление менеджерам о сдаче, пересборка вкладок геймы). 30с — короче
    # остальных интервалов намеренно: делегат ждёт быстрой реакции менеджеров.
    _add_interval_job(miniapp_outbox_drain_job, "miniapp_outbox_drain", timedelta(seconds=30))

    # ME-03: re-arm any pending broadcast whose date job was dropped from the jobstore during a
    # downtime longer than misfire_grace — otherwise it stays 'pending' forever and never fires.
    await reconcile_scheduled_broadcasts()
    # Quick 260822: дослать накопленные дайджесты сдач (services/game_digest.py). Ленивый
    # импорт — game_digest сам импортирует этот модуль.
    from services.game_digest import rearm_pending_digests
    await rearm_pending_digests()
    # Опросы: та же реконсиляция для отложенных/недосланных опросов (poll_{id} date jobs).
    await reconcile_scheduled_polls()
    # Nothing (interval or date) may fire until the whole schedule above is assembled.
    _scheduler.resume()
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
    startup. A job that is still present in the store is left untouched (no double-fire).

    Night review 260816 (review/services.md #4) — HOW that last promise is kept. Handing the
    executor a run older than `misfire_grace_time` makes it drop the run as a misfire
    (executors/base.py:117-127) and, a date job having no next run, the job is deleted: the row
    stayed 'pending' forever and every boot repeated the same silent drop. So a row older than
    `_MISFIRE_GRACE_SECONDS` is re-armed at now+1min instead — inside the grace, therefore it
    actually fires. Deliberate blast radius: such a broadcast DOES go out to its whole audience
    a minute after the restart, late. That is the documented trade — a late send beats a silent
    loss — and it is logged as a WARNING with the original time.

    The DB row is NOT rewritten: `scheduled_at` keeps what the manager typed, so the /scheduled
    screen (handlers/admin.py:2373, which prints the column verbatim) shows exactly what it
    showed before. Follow-up parked outside this fix: an `expired` status plus an admin screen
    «просроченные рассылки» with «отправить сейчас / отменить» instead of an automatic late send.
    """
    try:
        from database.db import list_pending_broadcasts, reclaim_stale_sending
        # Review 260817 §B2: a row stuck in 'sending' is a send that died mid-loop. Flip it
        # back to 'pending' so the same path below re-arms it; send_scheduled_broadcast skips
        # every chat already checkpointed in scheduled_broadcast_deliveries, so only the unsent
        # tail goes out. Fail-soft on its own so a DB hiccup here cannot block the pending pass.
        try:
            reclaimed = await reclaim_stale_sending(_STALE_SENDING_MINUTES)
            if reclaimed:
                logger.warning(
                    f"reconcile: reclaimed {len(reclaimed)} broadcast(s) stuck in 'sending' "
                    f"for >{_STALE_SENDING_MINUTES}m — resuming the unsent tail: {reclaimed}"
                )
        except Exception as e:
            logger.error(f"reconcile: reclaim_stale_sending failed: {e}")
        pending = await list_pending_broadcasts()
        sched = get_scheduler()
        recovered = 0
        # One "now" for the whole pass, so every row of a boot is measured from the same moment.
        now = _now_moscow_naive()
        for row in pending:
            bid = row["id"]
            if sched.get_job(f"bcast_{bid}") is not None:
                continue  # live job already scheduled — leave it
            try:
                run_at = datetime.strptime(row["scheduled_at"].strip(), "%Y-%m-%d %H:%M:%S")
            except (KeyError, TypeError, ValueError, AttributeError):
                logger.warning(f"reconcile: broadcast {bid} has unparseable scheduled_at — skipped")
                continue
            if (now - run_at).total_seconds() > _MISFIRE_GRACE_SECONDS:
                late_at = now + timedelta(minutes=1)
                logger.warning(
                    f"reconcile: broadcast {bid} scheduled at {row['scheduled_at']} is older "
                    f"than the misfire grace — sending late at {_fmt_dt(late_at)} instead of "
                    f"dropping it silently"
                )
                schedule_broadcast_job(bid, late_at)
            else:
                schedule_broadcast_job(bid, run_at)
            recovered += 1
        if recovered:
            logger.warning(f"Reconciled {recovered} pending broadcast(s) with dropped jobs")
    except Exception as e:
        logger.error(f"reconcile_scheduled_broadcasts failed: {e}")


# ── SCHED-01: scheduled-broadcast date job ───────────────────────────────────

# Night review 260815 (review/services.md #1): a send that dies with TelegramBadRequest
# ("chat not found", deleted account) is just as undeliverable as one that dies with
# TelegramForbiddenError — it only arrives as HTTP 400 instead of 403. Same class of bug the
# 14.08 quick fix `260813-833` closed in `admin_reply_to_question` (see
# handlers/admin.py::_PERMANENT_DELIVERY_ERRORS) — the pattern is copied, not reinvented.
#
# D-01: the WHOLE of TelegramBadRequest counts as permanent, deliberately. It is a wide class
# (it also covers "message is too long" / "can't parse entities"), but every caller here sends a
# FIXED payload to one chat_id: if the 400 came from the text rather than the chat, resending
# the same text to the same chat fails identically — the retry is useless under either reading.
# We do NOT sniff `e.message` for "chat not found": that pins us to Telegram's wording, which
# changes without notice. Transient failures (TelegramRetryAfter, TelegramNetworkError,
# timeouts) keep their own branches below and are still retried.
_PERMANENT_SEND_ERRORS = (TelegramForbiddenError, TelegramBadRequest)


async def _safe_send(send_coro_factory, chat_id, on_permanent_failure=None) -> bool:
    """Run one send with the 429-safe single-retry pattern (D-07/D-08).
    Returns True if delivered (first try or retry), False on genuine failure.

    `TelegramForbiddenError` (user blocked the bot / deactivated account) and
    `TelegramBadRequest` (chat unreachable / deleted account — "chat not found" arrives as
    HTTP 400) are PERMANENT failures: retrying them later can never succeed. Callers that
    would otherwise re-queue the same chat_id forever pass `on_permanent_failure` — an async
    callback run once so they can record the give-up. Without it a blocked user stayed a nudge candidate on every 15-minute
    scan (observed in production: 2934 + 2064 identical ERROR lines for two chat_ids).
    Logged at WARNING, not ERROR — it is a fact about the user, not a bot malfunction."""
    try:
        await send_coro_factory(chat_id)
        return True
    except _PERMANENT_SEND_ERRORS as e:
        logger.warning(f"Scheduled send permanently undeliverable for {chat_id}: {e}")
        if on_permanent_failure is not None:
            try:
                await on_permanent_failure(chat_id)
            except Exception as e2:
                logger.error(f"on_permanent_failure hook failed for {chat_id}: {e2}")
        return False
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
            list_delivered_chat_ids, mark_delivery, cleanup_deliveries,
        )
        row = await get_scheduled_broadcast(broadcast_id)
        if not row or row.get("status") != "pending":
            return
        # ME-02: atomically claim (pending → sending) BEFORE the send loop. If this returns 0
        # another fire already claimed it (double-schedule race), so bail. A crash mid-send
        # leaves it 'sending'; the boot reconciliation reclaims such rows (review 260817 §B2)
        # and this function runs again — idempotently, because every attempt is checkpointed
        # per recipient below and already-handled chats are skipped. The unsent tail is no
        # longer forfeited: it is resumed after the restart.
        if not await mark_broadcast_sending(broadcast_id):
            return

        filter_spec = row.get("filter_spec")
        if filter_spec:
            try:
                spec = json.loads(filter_spec)
                # WR-02: the `exclude` list inside an `event_city` filter is a SNAPSHOT of "the
                # other known city codes" taken when the manager built the filter. EVENT_CITIES
                # is an .env list edited between scheduling and sending, so a city added after
                # scheduling is missing from the frozen exclude — the default-city condition
                # (`event_city NOT IN (...)`) stops excluding it and its delegates leak into a
                # broadcast addressed to another city. Re-resolve against the LIVE registry at
                # send time; the stored exclude is only a fallback for pre-WR-02 rows.
                from cities import refresh_city_filter_spec
                spec = refresh_city_filter_spec(spec)
                if spec is None:
                    # An event_city filter names a code the registry no longer knows. Refuse:
                    # normalizing it would silently redirect the whole broadcast to the DEFAULT
                    # city, which is worse than not sending at all.
                    logger.error(
                        f"Scheduled broadcast {broadcast_id} targets an unknown event_city — "
                        "refusing to send (empty audience)"
                    )
                    target_ids = []
                else:
                    target_ids = await count_and_list_filtered(spec)
            except Exception as e:
                logger.error(f"Scheduled broadcast {broadcast_id} bad filter_spec: {e}")
                target_ids = []
        else:
            target_ids = await get_all_users_ids()

        text = row.get("text")
        photo = row.get("photo_file_id")
        # Checkpoint log from a previous (crashed) run: ok AND failed chats are skipped — a
        # failed one is a blocked/deactivated chat, re-hammering it on every resume is pointless.
        already = await list_delivered_chat_ids(broadcast_id)
        sent = skipped = failed = 0
        for chat_id in target_ids:
            if chat_id in already:
                skipped += 1
                continue
            if photo:
                ok = await _safe_send(
                    lambda cid: _bot.send_photo(cid, photo, caption=text), chat_id
                )
            else:
                ok = await _safe_send(lambda cid: _bot.send_message(cid, text), chat_id)
            await mark_delivery(broadcast_id, chat_id, bool(ok))
            if ok:
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)

        await mark_broadcast_sent(broadcast_id)
        logger.info(
            f"Scheduled broadcast {broadcast_id} done: sent {sent}, "
            f"skipped {skipped} (already), failed {failed} of {len(target_ids)}"
        )
        # The checkpoint rows only matter for resume; drop them once the row is 'sent'.
        try:
            await cleanup_deliveries(broadcast_id)
        except Exception as e:
            logger.warning(f"cleanup_deliveries({broadcast_id}): {e}")
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


# ── Опросы: отложенная отправка (зеркало scheduled broadcasts) ───────────────────────────────

async def send_scheduled_poll(poll_id: int):
    """Date-job target: рассылает опрос его аудитории. Аргумент — только int (picklable),
    бот — из модульного глобала. Клейм/чекпоинт/идемпотентность — внутри deliver_poll."""
    try:
        from services.polls import deliver_poll
        await deliver_poll(_bot, poll_id)
    except Exception as e:
        logger.error(f"send_scheduled_poll({poll_id}) failed: {e}")


def schedule_poll_job(poll_id: int, run_at: datetime):
    get_scheduler().add_job(
        send_scheduled_poll, "date",
        run_date=run_at, args=[poll_id],
        id=f"poll_{poll_id}", replace_existing=True,
    )


def cancel_poll_job(poll_id: int):
    try:
        get_scheduler().remove_job(f"poll_{poll_id}")
    except Exception as e:
        logger.warning(f"cancel_poll_job({poll_id}): {e}")


async def reconcile_scheduled_polls():
    """На буте: 'sending' старше порога → обратно в 'scheduled' (крах посреди рассылки),
    затем каждому 'scheduled' без живой джобы ставится джоба заново. Просроченные дальше
    misfire-грейса уходят через минуту (поздно лучше, чем никогда — та же сделка, что у
    рассылок). Fail-soft: никогда не блокирует старт."""
    try:
        from database.db import list_polls, reclaim_stale_sending_polls
        try:
            reclaimed = await reclaim_stale_sending_polls(_STALE_SENDING_MINUTES)
            if reclaimed:
                logger.warning(f"reconcile: reclaimed poll(s) stuck in 'sending': {reclaimed}")
        except Exception as e:
            logger.error(f"reconcile: reclaim_stale_sending_polls failed: {e}")
        sched = get_scheduler()
        now = _now_moscow_naive()
        recovered = 0
        for row in await list_polls(statuses=("scheduled",)):
            pid = row["id"]
            if sched.get_job(f"poll_{pid}") is not None:
                continue
            try:
                run_at = datetime.strptime((row.get("scheduled_at") or "").strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                logger.warning(f"reconcile: poll {pid} has unparseable scheduled_at — skipped")
                continue
            if (now - run_at).total_seconds() > _MISFIRE_GRACE_SECONDS:
                late_at = now + timedelta(minutes=1)
                logger.warning(
                    f"reconcile: poll {pid} scheduled at {row['scheduled_at']} is older than the "
                    f"misfire grace — sending late at {_fmt_dt(late_at)}"
                )
                schedule_poll_job(pid, late_at)
            else:
                schedule_poll_job(pid, run_at)
            recovered += 1
        if recovered:
            logger.warning(f"Reconciled {recovered} scheduled poll(s) with dropped jobs")
    except Exception as e:
        logger.error(f"reconcile_scheduled_polls failed: {e}")


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
        if await get_setting_typed("payment_reminders_enabled") != "on":  # REG-02: registry-backed
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
        from database.db import _connect
        # REG-02: read through the registry accessor — byte-identical to the previous
        # get_setting + _parse_schedule_dt pair (see tests/test_settings_consumers_phase6.py).
        deadline = await get_setting_typed("payment_deadline")
        if not deadline:
            return
        # TZFIX-260816: payment_deadline is admin-entered ДД.ММ.ГГГГ ЧЧ:ММ meaning Moscow
        # wall-clock time, so compare against Moscow wall-clock, not the container clock (UTC).
        if _now_moscow_naive() < deadline:
            return
        select_where = (
            "payment_status='not_paid' "
            "AND (payment_option IS NOT NULL OR payment_due IS NOT NULL)"
        )
        async with _connect() as db:
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
        if overdue_ids and await get_setting_typed("payment_reminders_enabled") == "on":  # REG-02: registry-backed
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
    """Interval-job target (no args, picklable). Full-refresh every «Незавершённые» sheet tab
    (one per city, Phase 07.1 CITY-04) with the current dropout list every
    incomplete_sync_hours. Fail-soft."""
    try:
        from services.sheets import sync_named_worksheet
        from handlers.registration import incomplete_city_batches
        # Phase 07.1 (CITY-04): incomplete_city_batches() is the SINGLE shared helper for both
        # this auto-sync and the admin-triggered export (handlers/admin.py::export_incomplete)
        # — headers are computed once inside it and rows are grouped by resolved tab name, so
        # the two callers can no longer drift (WR-01 parity, now extended to per-city tabs).
        # When adding columns to the «Незавершённые» tabs, edit only the helpers in
        # handlers/registration.py, not this job.
        batches = await incomplete_city_batches()
        for tab, headers, sheet_rows in batches:
            await sync_named_worksheet(tab, headers, sheet_rows)
    except Exception as e:
        logger.error(f"sync_incomplete_sheet_job failed: {e}")


async def miniapp_outbox_drain_job():
    """Interval-job target (no args, picklable — Pitfall 3: a job function must never close
    over a Bot). Delegates to services/miniapp_outbox.py::drain(bot), reading the injected
    bot from THIS module's own global — same shape as sweep_payment_overdue/
    nudge_incomplete_registrations above. Lazy import: services.miniapp_outbox imports
    services.game_digest, which imports this module at ITS top level (`from services import
    scheduler as _sched`) — importing it at OUR top level would run that import mid-module,
    before `_bot`/`_scheduler` exist yet."""
    try:
        from services.miniapp_outbox import drain
        await drain(_bot)
    except Exception as e:
        logger.error(f"miniapp_outbox_drain_job failed: {e}")


async def nudge_incomplete_registrations():
    """Interval-job target (no args, picklable). Nudge each incomplete registration
    older than the threshold exactly once, then stamp nudged_at (D-14)."""
    try:
        from database.db import get_nudge_candidates, mark_nudged
        if not _nudge_enabled(await get_setting("nudge_enabled")):
            return
        after_minutes = _int_or_default(await get_setting("nudge_after_minutes"), 120)
        # TZFIX-260816: deliberately NOT _now_moscow_naive() here. This compares against
        # reg_started.started_at, which the bot itself stamped via datetime.now() on the
        # container clock — both sides of the comparison are already the container clock, so
        # the invariant holds as-is. Switching to Moscow would be a REGRESSION: the ~129
        # already-recorded started_at rows would instantly "age" by 3 hours and nudges would
        # fire early. This is not the same bug class as scheduler.py:345 / admin.py:2312 /
        # admin.py:4711 (those compare against human input in Moscow time) — do not "fix" this
        # by symmetry. See .planning/TZFIX-260816.md.
        cutoff = _nudge_cutoff(datetime.now(), after_minutes)
        candidates = await get_nudge_candidates(cutoff)
        if not candidates:
            return
        text = await get_setting("nudge_text") or DEFAULT_NUDGE_TEXT
        # Phase 21 (21-09, D-21): вторая поверхность — «в чате» (deep-link ?start=continue) и
        # «📱 в приложении» (web_app, только при включённом разделе «📝 Анкета» и самом Mini
        # App). Построены ОДИН раз на весь прогон джобы, не на каждого делегата — get_me()/
        # тумблеры не меняются посреди одного цикла. nudge_text и mark_nudged НЕ трогаем; отбор
        # кандидатов ("активен в приложении сейчас" -> не в списке) уже сделан внутри
        # database.db.get_nudge_candidates (план 21-05) — второй копии этого условия здесь
        # быть не должно.
        kb = await _nudge_keyboard()
        for tid in candidates:
            # A blocked user can never receive the nudge, so stamping nudged_at on permanent
            # failure is what keeps the "exactly once" contract (D-14) from degenerating into
            # "forever" — the give-up is the one-shot.
            ok = await _safe_send(
                lambda cid: _bot.send_message(cid, text, reply_markup=kb), tid,
                on_permanent_failure=mark_nudged,
            )
            if ok:
                await mark_nudged(tid)  # one-shot only after a successful send
            await asyncio.sleep(0.05)
    except Exception as e:
        logger.error(f"nudge_incomplete_registrations failed: {e}")


async def _nudge_keyboard() -> InlineKeyboardMarkup | None:
    """Phase 21 (21-09, D-21): the догонялка's own two-button keyboard — «в чате» (always, if
    the bot's username is resolvable) and «📱 в приложении» (only when the delegate-facing
    form section AND the Mini App master toggle are both on and a public URL is configured).
    None (no keyboard at all) if even the bot username can't be resolved — the plain-text
    nudge still goes out, same as before this plan."""
    bot_username = None
    try:
        me = await _bot.get_me()
        bot_username = me.username
    except Exception as e:
        logger.warning(f"nudge: get_me failed, chat button omitted: {e}")
    if not bot_username:
        return None
    rows = [[InlineKeyboardButton(
        text=await get_setting_typed("reg_nudge_chat_button_text"),
        url=f"https://t.me/{bot_username}?start=continue",
    )]]
    try:
        if await get_setting_typed("miniapp_section_form") == "on" \
                and await get_setting_typed("miniapp_enabled") == "on" \
                and config.DASHBOARD_PUBLIC_URL:
            rows.append([InlineKeyboardButton(
                text=await get_setting_typed("reg_nudge_app_button_text"),
                web_app=WebAppInfo(url=config.DASHBOARD_PUBLIC_URL.rstrip("/") + "/app"),
            )])
    except Exception as e:
        logger.warning(f"nudge: app button build failed: {e}")
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── VERIF: mandatory allowlist-refresh interval job (D-11) ───────────────────

async def allowlist_refresh_job():
    """Interval-job target: reload the RAM allowlist; if it lands empty WHILE gating is
    ON, fire a loud admin alert (fail-open posture, owner-confirmed Open Q2)."""
    try:
        from services.allowlist import refresh_allowlist, allowlist_size
        # With gating OFF the cached set is never read, so refreshing it buys nothing and
        # costs a Sheets API call every hour — one that logs a WARNING forever when the
        # allowlist tab does not exist (the production default: no «Отобранные» tab).
        # Manual /refresh_allowlist stays available and still refreshes unconditionally.
        gating_on = (await get_setting("preselect_enabled") or "off") == "on"
        if not gating_on:
            logger.debug("Allowlist refresh skipped: preselect gating is off")
            return
        await refresh_allowlist()
        if allowlist_size() == 0:
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
