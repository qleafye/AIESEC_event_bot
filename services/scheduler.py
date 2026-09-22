"""Phase 3: persistent AsyncIOScheduler (SCHED-01) + interval jobs (SCHED-03, VERIF refresh).

Tech-stack lock (CLAUDE.md): APScheduler 3.x `AsyncIOScheduler` + `SQLAlchemyJobStore`
on a SEPARATE sqlite file (data/jobs.sqlite), never forum.db (Pitfall 2). Do NOT use the
APScheduler-4.0 API, the in-memory job store, or the thread-based (background) scheduler
— see CLAUDE.md "What NOT to Use".

Job targets are module-level coroutines taking only picklable primitives (an int id /
no args) — never a Bot/closure (Pitfall 3). The Bot is injected once via a module global.
"""
import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import config
from database.db import get_setting
from settings_schema import get_setting_typed
from services.timeutil import MOSCOW_TZ, msk_now

logger = logging.getLogger(__name__)

_JOBSTORE_URL = "sqlite:///data/jobs.sqlite"

# TZFIX-260816: the scheduler pin below (init_scheduler) and _now_moscow_naive() read the SAME
# MOSCOW_TZ constant, so they structurally cannot drift apart — that drift (pin=Moscow,
# checks=container clock/UTC) was exactly the bug this fix closes. See .planning/TZFIX-260816.md.
# Quick 260904-kk6 (Q1): the literal itself now lives in `services/timeutil.py` (leaf module,
# no aiogram import) — `services/questions.py::format_stamp` needs it too and cannot import
# this module (aiogram + APScheduler), and the "exactly one literal" guard
# (tests/test_timezone_fix_260816.py::test_moscow_literal_declared_exactly_once) forbids a
# second copy. This is a re-export, not a second source of truth.

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

    Quick 260912-mcj: семья меток времени, которую бот сам стамповал (`reg_started.started_at`
    и вся остальная семья naive-local в `database/db.py`), теперь ТОЖЕ московская
    (`services.timeutil.msk_now()`), а не часы контейнера — поэтому сравнивать с ней НУЖНО
    именно этим хелпером (см. `_nudge_cutoff` ниже — прежний запрет снят вместе со сменой
    зоны хранения).
    """
    return msk_now()


def _nudge_cutoff(now: datetime, minutes: int) -> str:
    """now minus `minutes`, ISO-formatted — the started_at threshold for the scan.

    Quick 260912-mcj: `reg_started.started_at` теперь пишется московским `msk_now()` (раньше —
    часами контейнера/UTC на проде), а старые строки сдвинуты одноразовой миграцией квика на
    +3 часа. Обе стороны сравнения обязаны быть на одних часах — `now` сюда приходит из
    `_now_moscow_naive()` (см. вызов в `nudge_incomplete_registrations`). Инверсия решения
    TZFIX-260816 (тогда обе стороны держали на часах контейнера) — не регрессия, а следствие
    смены зоны хранения самой колонки.
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


def get_bot():
    """Тот же Bot, что схватил `init_scheduler` — единственный внешний доступ к модульному
    `_bot` (Pitfall 3: job-таргеты этого файла берут Bot из `_bot` напрямую, а не как
    аргумент). Нужен `chat_tracking.bind_reconcile_job` (квик 260915-twr, D2) — job-таргет
    ДРУГОГО модуля, которому Bot нужен без протаскивания его через picklable-аргументы
    APScheduler; сам `chat_tracking.py` держит импорт этого модуля ленивым (докстринг :1-17,
    aiogram-free)."""
    if _bot is None:
        raise RuntimeError("Scheduler not initialised — call init_scheduler(bot) first")
    return _bot


def _add_interval_job(func, job_id: str, interval: timedelta, *,
                       first_run_delay: timedelta | None = None):
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
        interval) — UNLESS `first_run_delay` is given (fourth rule below).
      * (квик 260915-twr, D3) no job yet / interval changed, AND `first_run_delay` is passed ->
        `next_run_time = now + first_run_delay`. For a job whose very first run should not
        wait a full interval (e.g. a 6h chat-membership sweep that should also run 2 minutes
        after boot instead of 6 hours after it). Never applies to the "same interval, reuse
        saved schedule" branch above — the whole point of this function is to NOT overwrite a
        schedule that already lives in the jobstore, and `first_run_delay` only fires the FIRST
        time a job is registered (or after the manager changes its interval).

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
    elif first_run_delay is not None:
        kwargs["next_run_time"] = datetime.now(MOSCOW_TZ) + first_run_delay
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

    # Quick 260907-4ai (P0 SkillUp5): догрузка резюме, не улетевшего в Nextcloud на финале
    # (облако лежало/таймаут) — джоба сама молчит, когда Nextcloud не настроен, поэтому
    # отдельного тумблера нет.
    retry_minutes = _int_or_default(await get_setting("resume_retry_minutes"), 10)
    _add_interval_job(resume_upload_retry_job, "resume_upload_retry", timedelta(minutes=retry_minutes))

    # Phase 19 (08, D-01/Pattern 7): разбор miniapp_outbox — побочные эффекты записи из
    # Mini App (уведомление менеджерам о сдаче, пересборка вкладок геймы). 30с — короче
    # остальных интервалов намеренно: делегат ждёт быстрой реакции менеджеров.
    _add_interval_job(miniapp_outbox_drain_job, "miniapp_outbox_drain", timedelta(seconds=30))

    # Phase 27 (27-03, LANG-04): разбор очереди перевода делегатской анкеты. 30с — тот же
    # интервал, что у miniapp_outbox выше (батч ограничен services/i18n_worker.py::BATCH_SIZE,
    # инференс — в отдельном потоке, длинный батч не морозит long polling ни на одном тике).
    _add_interval_job(translation_drain_job, "translation_drain", timedelta(seconds=30))

    # Quick 260904-dq1: разбор очереди «🌙 Тихие часы» — каждую минуту, ре-арм на старте не
    # нужен (см. докстринг quiet_hours_flush_job).
    _add_interval_job(quiet_hours_flush_job, "quiet_hours_flush", timedelta(minutes=1))

    # Квик 260914-rgr (RGR-01..07): периодическая сверка состава чата делегатов с Telegram.
    # Дефолт интервала — 360 мин (6 часов), тот же приём, что у остальных интервалов джоб выше.
    # Квик 260915-twr (D3): первый прогон — через _BOOT_CATCHUP (2 мин) после старта, а не
    # через полный интервал — иначе новопривязанный чат молчит до 6 часов; сохранённое в
    # jobstore расписание уже заведённой джобы это не трогает (см. докстринг _add_interval_job).
    chat_refresh_minutes = _int_or_default(await get_setting("chat_refresh_minutes"), 360)
    _add_interval_job(
        chat_membership_refresh_job, "chat_membership_refresh",
        timedelta(minutes=chat_refresh_minutes),
        first_run_delay=_BOOT_CATCHUP,
    )

    # Квик 260916: «📊 Итоги дня» — ОДНА cron-джоба на весь бот, время из реестра
    # (daily_digest_time, ЧЧ:ММ МСК). Регистрируется на каждом старте с replace_existing, как
    # интервальные соседи выше, поэтому новое время начинает действовать после перезапуска —
    # ровно это и написано менеджеру в подсказке ключа. Сам тумблер джоба перечитывает у себя
    # внутри: выключенный даёт ранний выход, снимать джобу не нужно.
    from services.daily_digest import JOB_ID as _DIGEST_JOB_ID, daily_digest_job, parse_time
    _digest_hour, _digest_minute = parse_time(await get_setting_typed("daily_digest_time"))
    _scheduler.add_job(
        daily_digest_job, "cron", hour=_digest_hour, minute=_digest_minute,
        id=_DIGEST_JOB_ID, replace_existing=True,
    )

    # ME-03: re-arm any pending broadcast whose date job was dropped from the jobstore during a
    # downtime longer than misfire_grace — otherwise it stays 'pending' forever and never fires.
    await reconcile_scheduled_broadcasts()
    # Quick 260822: дослать накопленные дайджесты сдач (services/game_digest.py). Ленивый
    # импорт — game_digest сам импортирует этот модуль.
    from services.game_digest import rearm_pending_digests
    await rearm_pending_digests()
    # Квик 260916: то же самое для дайджеста ЗАЯВОК (services/reg_digest.py) — своя очередь,
    # свои джобы reg_digest:{city}. Алиас при импорте: имя функции у обоих модулей одно и то
    # же (родные братья), а второй импорт затёр бы первый.
    from services.reg_digest import rearm_pending_digests as rearm_pending_reg_digests
    await rearm_pending_reg_digests()
    # Опросы: та же реконсиляция для отложенных/недосланных опросов (poll_{id} date jobs).
    await reconcile_scheduled_polls()
    # Phase 32 (32-08, T-32-08-07): то же самое для трёх джоб амбассадорских волн (старт,
    # напоминание о дедлайне, конец волны) — вызывается ПОСЛЕДНЕЙ из реконсиляций namespace'а
    # (после опросов), тот же порядок, что у остальных «дослать пропущенное на старте» шагов.
    await reconcile_wave_jobs()
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
            create_broadcast, set_scheduled_log_broadcast_id,
            record_broadcast_delivery, finish_broadcast,
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
                    # Квик 260914-rgr (RGR-01..07): та же забота, что у event_city выше —
                    # карта `chats` внутри спеки delegate_chat пересобирается ЗАНОВО на
                    # момент отправки, а не хранится замороженной со времени планирования.
                    # Выбрано пересобирать (не отдельный `refresh_*_spec`, как у городов) —
                    # проще и честнее: чат, отвязанный между планированием и отправкой, не
                    # должен давать аудиторию «все», а замороженная карта именно так бы и
                    # сделала (пустой EXISTS -> фрагмент "0" -> пустая, но НЕ ошибочная
                    # аудитория есть только если карта пуста целиком, а не устарела).
                    if any(isinstance(f, dict) and f.get("field") == "delegate_chat" for f in spec):
                        from cities import city_scope as _city_scope
                        from services.chat_tracking import bound_chats

                        bound = await bound_chats()
                        chats_payload = []
                        for entry in bound:
                            sc = _city_scope(entry["city"]) if entry["city"] else None
                            chats_payload.append({
                                "city": entry["city"], "chat_id": entry["chat_id"],
                                "exclude": list(sc[1]) if sc else [],
                            })
                        spec = [
                            {**f, "chats": chats_payload}
                            if isinstance(f, dict) and f.get("field") == "delegate_chat" else f
                            for f in spec
                        ]
                    target_ids = await count_and_list_filtered(spec)
            except Exception as e:
                logger.error(f"Scheduled broadcast {broadcast_id} bad filter_spec: {e}")
                target_ids = []
        else:
            target_ids = await get_all_users_ids()

        # Квик 260915-twr (Task B1): один журнал, одна таблица broadcast_deliveries — отложенная
        # рассылка заводит (или переиспользует после рестарта) ту же строку `broadcasts`, что
        # мгновенная. `broadcast_id` в этой функции — id строки `scheduled_broadcasts`
        # (им владеют mark_delivery/cleanup_deliveries — чекпоинт идемпотентности повтора);
        # `log_bid` — id строки `broadcasts` (им владеют record_broadcast_delivery/
        # finish_broadcast — журнал «Последние рассылки» и отзыв). Не перепутать.
        log_bid = row.get("log_broadcast_id")
        if not log_bid:
            log_bid = await create_broadcast(
                row.get("created_by") or 0, (row.get("text") or "")[:80], len(target_ids)
            )
            await set_scheduled_log_broadcast_id(broadcast_id, log_bid)

        text = row.get("text")
        photo = row.get("photo_file_id")
        # Checkpoint log from a previous (crashed) run: ok AND failed chats are skipped — a
        # failed one is a blocked/deactivated chat, re-hammering it on every resume is pointless.
        already = await list_delivered_chat_ids(broadcast_id)
        sent = skipped = failed = 0
        # Собираем message_id доставленных сообщений для журнала (record_broadcast_delivery).
        # _safe_send отдаёт только True/False и менять её нельзя (её зовут ещё три джобы) —
        # поэтому сама фабрика отправки кладёт id в этот словарь по chat_id. Ретрай-ветка
        # _safe_send вызовет фабрику повторно — словарь перезапишется актуальным id, это
        # правильное поведение.
        sent_message_ids: dict[int, int] = {}
        for chat_id in target_ids:
            if chat_id in already:
                skipped += 1
                continue
            if photo:
                async def _send(cid, _photo=photo, _text=text):
                    msg = await _bot.send_photo(cid, _photo, caption=_text)
                    sent_message_ids[cid] = msg.message_id
                    return msg
                ok = await _safe_send(_send, chat_id)
            else:
                async def _send(cid, _text=text):
                    msg = await _bot.send_message(cid, _text)
                    sent_message_ids[cid] = msg.message_id
                    return msg
                ok = await _safe_send(_send, chat_id)
            await mark_delivery(broadcast_id, chat_id, bool(ok))
            if ok and chat_id in sent_message_ids:
                # fail-soft: отсутствие id (странный ответ API) не должно ронять рассылку
                await record_broadcast_delivery(log_bid, chat_id, sent_message_ids[chat_id])
            if ok:
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)

        await mark_broadcast_sent(broadcast_id)
        # sent + skipped: skipped — доставленные ПРЕДЫДУЩИМ прогоном после рестарта, для
        # менеджера они доставлены не меньше, чем sent из этого прогона.
        await finish_broadcast(log_bid, "done", sent + skipped, failed)
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
    module global. SC#5: never fire if already paid or a receipt is already in review.

    Quick 260904-dq1 (mechanism 1 of 3 — see the module comment above sweep_payment_overdue
    for why the three reminder jobs each pick a DIFFERENT mechanism): gated on quiet hours at
    FIRE time, same idiom as the `payment_reminders_enabled` gate right above it — a
    one-shot date job can cheaply RESCHEDULE ITSELF onto the end of the window instead of
    firing. The re-added job uses a NEW id (not the original `pay_reminder_{user_id}_{label}`
    — this function only receives `user_id`, the label isn't picklable through here); this is
    safe because the self-guard above (paid/receipt_sent/None -> return) makes a stray extra
    job a harmless no-op even if `cancel_payment_reminders` (fired on receipt confirm) never
    learns this new id and cannot cancel it."""
    try:
        from database.db import get_user
        # Gated at FIRE time (not scheduling) so the admin toggle takes effect live — even
        # on reminders already sitting in the jobstore. Default on = prior behaviour.
        if await get_setting_typed("payment_reminders_enabled") != "on":  # REG-02: registry-backed
            return
        user = await get_user(user_id)
        if not user or user.get("payment_status") in ("paid", "receipt_sent", None):
            return
        from services import quiet_hours
        now = _now_moscow_naive()
        due = await quiet_hours.defer_until(now, user_id)
        if due is not None:
            get_scheduler().add_job(
                send_payment_reminder, "date", run_date=due, args=[user_id],
                id=f"pay_reminder_{user_id}_quiet_hours_shift", replace_existing=True,
            )
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
    a parseable payment_deadline is set and has passed.

    Quick 260904-dq1 (mechanism 2 of 3 — queue, not self-reschedule): this is a DAILY job —
    sliding its own next run to the end of quiet hours (mechanism 1 above, `send_payment_
    reminder`'s date-job self-reschedule) would mean the users it flips today wait until the
    NEXT DAY's run to be re-selected, since the status flip itself must stay immediate (it
    feeds the «неоплатившие» broadcast segment). So the status flip runs unconditionally, and
    only the one-shot ping below goes through `quiet_hours.send_or_queue_text` (mechanism 3,
    skip-without-stamp, is for `nudge_incomplete_registrations` below — a different job with a
    single-shot budget that a queued send would spend for nothing)."""
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
            from services import quiet_hours
            for tid in overdue_ids:
                await quiet_hours.send_or_queue_text(
                    _now_moscow_naive(), tid, text,
                    sender=lambda cid=tid: _bot.send_message(cid, text),
                )
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


async def _flush_due_application_decisions(now: datetime) -> None:
    """Phase 23 (23-04, D-06/T-23-18): crash-safety sweep — if the web process died inside
    the undo window, the decision's deferred effect (`application_decided`) would otherwise
    never leave `application_decisions` for `miniapp_outbox`. Same translation as
    `miniapp/outbox.py::flush_application_decisions` (kind/payload shape, collect-then-gather
    around `services.applications.flush_due_decisions`'s sync unawaited callback) — duplicated
    here on purpose rather than imported: `services/scheduler.py` (bot process) must never
    depend on `miniapp.*` (the one-way boundary every other job in this file already respects,
    see the docstring above). Lazy import of `services.applications`/`database.db.
    enqueue_miniapp_outbox` for the same reason the sibling job below stays lazy — this
    function only runs from inside a job, never at module import time."""
    from database.db import enqueue_miniapp_outbox
    from services import applications

    pending = []
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    def _collect(decision: str, row: dict) -> None:
        payload = {
            "telegram_id": row["telegram_id"],
            "status": row.get("decision", decision),
            "reason": row.get("reason"),
        }
        pending.append(enqueue_miniapp_outbox("application_decided", payload, now_str))

    await applications.flush_due_decisions(now, _collect)
    if pending:
        await asyncio.gather(*pending)


async def miniapp_outbox_drain_job():
    """Interval-job target (no args, picklable — Pitfall 3: a job function must never close
    over a Bot). Delegates to services/miniapp_outbox.py::drain(bot), reading the injected
    bot from THIS module's own global — same shape as sweep_payment_overdue/
    nudge_incomplete_registrations above. Lazy import: services.miniapp_outbox imports
    services.game_digest, which imports this module at ITS top level (`from services import
    scheduler as _sched`) — importing it at OUR top level would run that import mid-module,
    before `_bot`/`_scheduler` exist yet.

    Phase 23 (23-04, D-06): runs `_flush_due_application_decisions` FIRST — the web process's
    own request-time/background sweep (`miniapp/outbox.py::flush_application_decisions`)
    normally beats this job to it, this is only the safety net for a web process that died
    inside the undo window (known limit: effect delay then grows from 5s to <= this job's
    30s interval, documented in the plan's SUMMARY)."""
    try:
        await _flush_due_application_decisions(_now_moscow_naive())
        from services.miniapp_outbox import drain
        await drain(_bot)
    except Exception as e:
        logger.error(f"miniapp_outbox_drain_job failed: {e}")


async def translation_drain_job():
    """Interval-job target (no args, picklable — Pitfall 3), ровно по образцу
    `miniapp_outbox_drain_job` выше. Выключенный модуль (`delegate_lang_enabled` != "on",
    A-05 27-CONTEXT.md) выходит НЕМЕДЛЕННО, не читая очередь ни разу — 30-секундный тик не
    должен стоить ни одного запроса, пока делегатский английский не включён. Ленивый импорт
    `services.i18n_worker` внутри функции — тот же приём, что у соседей (сам
    `services.i18n_worker` лениво импортирует `argostranslate` только внутри драйвера, не
    здесь и не при импорте этого модуля)."""
    try:
        if await get_setting_typed("delegate_lang_enabled") != "on":
            return
        from services.i18n_worker import drain
        await drain()
    except Exception as e:
        logger.error(f"translation_drain_job failed: {e}")


async def resume_upload_retry_job():
    """Interval-job target (no args, picklable — Pitfall 3), ровно по образцу
    `translation_drain_job`/`quiet_hours_flush_job` выше. Ленивый импорт
    `services.reg_finalize` (сам джоба-модуль ничего не знает про Nextcloud/Sheets —
    догрузку резюме, не улетевшего в облако на финале). Джоба сама молчит, когда Nextcloud
    не настроен (`nextcloud.is_configured()` проверяется внутри `retry_pending_resume_uploads`
    ДО любого обращения к БД/боту) — отдельного тумблера на эту джобу поэтому нет."""
    try:
        from services.reg_finalize import retry_pending_resume_uploads
        await retry_pending_resume_uploads(_bot)
    except Exception as e:
        logger.error(f"resume_upload_retry_job failed: {e}")


async def quiet_hours_flush_job():
    """Interval-job target (no args, picklable — Pitfall 3), ровно по образцу
    `miniapp_outbox_drain_job` выше. Ленивый импорт `services.quiet_hours` (aiogram-free
    модуль, его же импортирует веб-процесс — не тащить его собственный импорт в верхний
    уровень этого файла было бы поводом для цикла, если бы quiet_hours когда-нибудь захотел
    что-то отсюда). Ре-арм на старте не нужен: interval-джоба взводится каждым бутом, а
    просроченные строки разбираются первым же тиком по `due_at <= now`."""
    try:
        from services import quiet_hours
        await quiet_hours.flush_due(_now_moscow_naive())
    except Exception as e:
        logger.error(f"quiet_hours_flush_job failed: {e}")


async def _nudge_remaining_for(tid: int) -> int | None:
    """Phase 28 (28-09, SU-09, Pitfall 6): остаток вопросов для ОДНОГО кандидата — свой
    `get_reg_draft` + `reg_engine.enabled_steps` внутри цикла (не пересчёт по батчу разом,
    иначе все делегаты получили бы одно и то же число). Зовётся ТОЛЬКО когда `nudge_text`
    реально содержит `{remaining}` (T-28-09-01) — редкий батч, но лишний запрос на кандидата
    без надобности не тратим.

    `None` — черновика нет или остаток посчитать не удалось; вызывающий подставляет
    нейтральное слово вместо числа, а не пустые фигурные скобки (T-28-09-04)."""
    try:
        from database.db import get_reg_draft
        import reg_engine
        draft = await get_reg_draft(tid)
        if not draft:
            return None
        answers = draft.get("answers") or {}
        enabled = await reg_engine.enabled_steps(answers, draft.get("event_city"))
        return sum(1 for step_key in enabled if not answers.get(step_key))
    except Exception as e:
        logger.warning(f"nudge remaining count failed for {tid}: {e}")
        return None


async def nudge_incomplete_registrations():
    """Interval-job target (no args, picklable). Nudge each incomplete registration
    older than the threshold exactly once, then stamp nudged_at (D-14).

    Quick 260904-dq1 (mechanism 3 of 3 — skip without stamping, no queue): a candidate caught
    in quiet hours is skipped WITHOUT calling `mark_nudged` — this job re-scans every
    `nudge_scan_minutes` (default 15) and will pick the same candidate up again right after
    the window ends. A queue entry here would be the wrong tool twice over: it would burn the
    one-shot `mark_nudged` budget (D-14) on a message that hasn't gone out yet, and the delay
    is short enough (one scan tick past the window) that a persisted row buys nothing.

    Phase 28 (28-09, SU-09): `nudge_text` без `{remaining}` не читает ни одного черновика —
    байт-в-байт прежнее поведение. С плейсхолдером текст ПОДСТАВЛЯЕТСЯ (`.replace`, не
    `.format` — текст менеджера может содержать посторонние фигурные скобки, T-28-09-04)
    заново на КАЖДОГО кандидата внутри цикла (`_nudge_remaining_for`), сам `text`
    (общий, прочитанный один раз) не перезаписывается."""
    try:
        from database.db import get_nudge_candidates, mark_nudged
        if not _nudge_enabled(await get_setting("nudge_enabled")):
            return
        after_minutes = _int_or_default(await get_setting("nudge_after_minutes"), 120)
        # Quick 260912-mcj: инверсия TZFIX-260816. Раньше `reg_started.started_at` писался
        # часами контейнера (UTC на проде) и обе стороны сравнения намеренно держали на
        # container clock. С этого квика колонка пишется московским `msk_now()`, а все старые
        # строки сдвинуты одноразовой миграцией на +3 часа — значит и «сейчас» здесь обязано
        # быть московским, иначе разъезд на 3 часа возникнет в обратную сторону. См.
        # `_nudge_cutoff` и `_now_moscow_naive` выше.
        cutoff = _nudge_cutoff(_now_moscow_naive(), after_minutes)
        candidates = await get_nudge_candidates(cutoff)
        if not candidates:
            return
        text = await get_setting("nudge_text") or DEFAULT_NUDGE_TEXT
        has_remaining_placeholder = "{remaining}" in text
        # Phase 21 (21-09, D-21): вторая поверхность — «в чате» (deep-link ?start=continue) и
        # «📱 в приложении» (web_app, только при включённом разделе «📝 Анкета» и самом Mini
        # App). Построены ОДИН раз на весь прогон джобы, не на каждого делегата — get_me()/
        # тумблеры не меняются посреди одного цикла. nudge_text и mark_nudged НЕ трогаем; отбор
        # кандидатов ("активен в приложении сейчас" -> не в списке) уже сделан внутри
        # database.db.get_nudge_candidates (план 21-05) — второй копии этого условия здесь
        # быть не должно.
        kb = await _nudge_keyboard()
        from services import quiet_hours
        now = _now_moscow_naive()
        for tid in candidates:
            if await quiet_hours.defer_until(now, tid) is not None:
                continue  # тихие часы -- пропуск без mark_nudged, заберёт следующий тик
            msg_text = text
            if has_remaining_placeholder:
                remaining = await _nudge_remaining_for(tid)
                if remaining is None:
                    fallback = await get_setting_typed("nudge_remaining_fallback_text")
                    msg_text = text.replace("{remaining}", fallback)
                else:
                    msg_text = text.replace("{remaining}", str(remaining))
            # A blocked user can never receive the nudge, so stamping nudged_at on permanent
            # failure is what keeps the "exactly once" contract (D-14) from degenerating into
            # "forever" — the give-up is the one-shot.
            ok = await _safe_send(
                lambda cid, mt=msg_text: _bot.send_message(cid, mt, reply_markup=kb), tid,
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


async def chat_membership_refresh_job():
    """Interval-job target: периодическая сверка состава чата(ов) делегатов с Telegram.
    Тумблер выключен -> ранний выход без единого вызова `get_chat_member` (та же форма, что
    `allowlist_refresh_job` с `preselect_enabled` выше). Ленивый импорт — `services.chat_tracking`
    сама ничего из `services/scheduler.py` не импортирует, но порядок импорта модулей внутри
    `services/` держим единообразно ленивым для job-таргетов (тот же приём везде в этом файле)."""
    try:
        from services.chat_tracking import tracking_on, refresh_all_chats

        if not await tracking_on():
            logger.debug("Chat membership refresh skipped: chat_tracking_enabled is off")
            return
        await refresh_all_chats(_bot)
    except Exception as e:
        logger.error(f"chat_membership_refresh_job failed: {e}")


# ── Phase 32 (32-08, D-11/D-26/D-30): джобы амбассадорских волн ──────────────────────────
# Тот же приём, что у напоминаний об оплате выше (schedule_payment_reminder/send_payment_
# reminder, ~строка 683): разовая `date`-джоба, детерминированный id, `replace_existing=True`,
# цель принимает только int-аргументы (picklable, Pitfall 3) и перечитывает живое состояние
# ПЕРЕД отправкой — джоба, поставленная неделю назад, исполняется в мире, которого не было
# при постановке (T-32-08-01/T-32-08-03).
#
# ОДНА джоба НА ВОЛНУ (не пер-амбассадорский фан-аут на постановке): круг получателей
# разворачивается в момент СРАБАТЫВАНИЯ (`send_wave_start`), а не в момент постановки
# (`schedule_wave_start_for_all`). Иначе амбассадор, вступивший ПОСЛЕ активации волны, но ДО
# её `starts_at`, — полноправный участник по `wave_eligible` — не получал бы стартового
# сообщения никогда: джобы на него не было, а `reconcile_wave_jobs` пропускает волны с уже
# выставленной `started_notified_at`. Тот же эффект чинит вторую половину бага: `started_
# notified_at` теперь ставится при ОТПРАВКЕ, а не при постановке — до этого она вставала
# сразу после активации волны и запирала правку дат (`wave_editable_fields`) ещё до того, как
# кто-либо что-либо получил.

async def _wave_eligible_ambassador_ids(wave: dict) -> list[int]:
    """Круг амбассадоров, подходящих волне `wave` ПРЯМО СЕЙЧАС (`wave_eligible`) — общий
    помощник для информативного числа на постановке (`schedule_wave_start_for_all`) и
    реального фан-аута на отправке (`send_wave_start`). `cities.city_scope(...)` ОБЯЗАТЕЛЕН
    — `wave["event_city"]` без обёртки роняет `database.db._city_clause` (`code, exclude =
    scope` на голой строке)."""
    from database.db import list_ambassadors
    from services.ambassador_waves import wave_eligible
    import cities

    ambassadors = await list_ambassadors(city_scope=cities.city_scope(wave.get("event_city")))
    ids: list[int] = []
    for a in ambassadors:
        user = dict(a)
        user["is_ambassador"] = 1  # list_ambassadors уже отфильтровал WHERE is_ambassador = 1
        if wave_eligible(user, wave):
            ids.append(int(a["telegram_id"]))
    return ids


def schedule_wave_start(wave_id: int, run_at: datetime) -> None:
    """Разовая джоба старта ОДНОЙ волны, id `wave_start_{wave_id}`. Повторная постановка
    (переармирование при рестарте, правка дат активной волны до отправки) заменяет джобу, а
    не плодит вторую — `replace_existing=True`, тот же приём, что у `schedule_payment_
    reminder`."""
    get_scheduler().add_job(
        send_wave_start, "date", run_date=run_at, args=[wave_id],
        id=f"wave_start_{wave_id}", replace_existing=True,
    )


def cancel_wave_jobs(wave_id: int) -> None:
    """Снимает джобу старта волны, джобу конца волны и джобу рассылки итогов. Каждое снятие в
    своём `try/except` (та же идиома, что `cancel_payment_reminders`) — джоба уже сработала
    или её вовсе не было, оба случая нормальные, не ошибка вызывающего.

    IN-07 (32-REVIEW.md): `wave_results_broadcast_{wave_id}` (`schedule_wave_results_
    broadcast`) раньше не входил в этот список — удаление волны (`wave_delete_go`) не снимало
    джобу рассылки итогов, если она успела встать до удаления (объявили — сразу удалили);
    джоба переживала волну и на сработавшем таймере читала уже несуществующий `wave_id` из
    БД, ничего не находила и не отправляла, но не собиралась вовсе."""
    sched = get_scheduler()
    for job_id in (
        f"wave_start_{wave_id}", f"wave_end_{wave_id}", f"wave_results_broadcast_{wave_id}",
    ):
        try:
            sched.remove_job(job_id)
        except Exception:
            pass


async def schedule_wave_start_for_all(wave_id: int) -> int:
    """Ставит ОДНУ джобу старта волны (D-30) на `starts_at`; если он уже прошёл (волна
    создана и сразу пущена, либо переармирование после простоя бота) — «сейчас + минута»,
    чтобы попасть в окно `_MISFIRE_GRACE_SECONDS`, а не молча пропустить рассылку. Круг
    получателей сюда больше не входит — это `send_wave_start`, срабатывающая на самой
    отправке (см. комментарий к разделу выше про главный баг фикса). Имя функции сохранено —
    её импортируют `handlers/admin_game_waves.py` и мокают тесты. Возвращаемое число — сколько
    амбассадоров подходят волне ПРЯМО СЕЙЧАС; чисто информативно, вызывающий код его не
    использует."""
    from database.db import get_wave

    wave = await get_wave(wave_id)
    if not wave:
        return 0

    now = _now_moscow_naive()
    try:
        starts_dt = datetime.strptime(wave["starts_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, KeyError):
        starts_dt = now
    run_at = starts_dt if starts_dt > now else now + timedelta(minutes=1)
    schedule_wave_start(wave_id, run_at)

    return len(await _wave_eligible_ambassador_ids(wave))


async def send_wave_start(wave_id: int) -> None:
    """Date-job target: единственная джоба старта ОДНОЙ волны (D-30). Круг получателей
    разворачивается ЗДЕСЬ, на срабатывании, а не на постановке — вступивший в амбассадоры
    ПОСЛЕ активации волны, но ДО её `starts_at`, тоже участник (`wave_eligible` это уже
    разрешала), и теперь действительно получает сообщение. Волны нет или она не `active` —
    молча выходим. `starts_at` сдвинули вперёд после постановки — переставляем джобу на новый
    момент (тот же приём, что `send_task_deadline_reminder`) и выходим, не рассылая рано.

    `mark_wave_started` ставится ДО фан-аута сознательно: при падении посреди рассылки лучше
    недослать хвосту получателей, чем задвоить всем после рестарта. Метка атомарна (`WHERE
    started_notified_at IS NULL`) — повторное срабатывание/переармирование той же волны не
    проходит и не шлёт ничего."""
    try:
        from database.db import get_wave, mark_wave_started

        wave = await get_wave(wave_id)
        if not wave or wave.get("state") != "active":
            return

        now = _now_moscow_naive()
        try:
            starts_dt = datetime.strptime(wave["starts_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, KeyError):
            starts_dt = now
        if starts_dt > now:
            schedule_wave_start(wave_id, starts_dt)
            return

        if not await mark_wave_started(wave_id, now.strftime("%Y-%m-%d %H:%M:%S")):
            return  # уже разослано — повторное срабатывание/переармирование, тишина

        for ambassador_id in await _wave_eligible_ambassador_ids(wave):
            await send_wave_start_dm(wave_id, ambassador_id)
            await asyncio.sleep(0.05)  # та же пауза между отправками, что в broadcast_run.py
    except Exception as e:
        logger.error(f"send_wave_start({wave_id}) failed: {e}")


async def send_wave_start_dm(wave_id: int, ambassador_id: int) -> None:
    """Отправка ОДНОГО стартового сообщения волны ОДНОМУ получателю — вызывается из фан-аута
    `send_wave_start` (D-30). Аргументы — только int. Перечитывает живое состояние ПЕРЕД
    отправкой (T-32-08-01): волна должна существовать и не быть в 'draft', получатель —
    по-прежнему амбассадор и участник ИМЕННО этой волны (`wave_eligible`). Любое из условий не
    выполнено — молча выходим, ничего не отправляя (вышедший из амбассадоров не получает
    ничего — D-30/D-32); свой try/except — сбой одного получателя (например, заблокировал
    бота) не обрывает фан-аут остальным."""
    try:
        from database.db import get_wave, get_user, list_wave_tasks, task_title
        from services.ambassador_waves import wave_eligible
        from services import quiet_hours, i18n
        import game_labels

        wave = await get_wave(wave_id)
        if not wave or wave.get("state") == "draft":
            return
        user = await get_user(ambassador_id)
        if not user or not wave_eligible(dict(user), wave):
            return

        tasks = await list_wave_tasks(wave_id, active_only=True)
        lang, tr_map = await i18n.context(ambassador_id)

        lines = []
        for t in tasks:
            # WR-12: `game_labels.task_deadline_text` уже возвращает готовое «без срока» для
            # задания без дедлайна — приклеивать «до » нужно ТОЛЬКО когда срок реально есть,
            # иначе делегат видит «до без срока».
            if game_labels.task_has_deadline(t):
                deadline_tail = f"до {await game_labels.task_deadline_text(t)}"
            else:
                deadline_tail = await game_labels.task_deadline_text(t)
            lines.append(
                f"• {html.escape(str(task_title(t)))} — {int(t['coins'])} баллов, {deadline_tail}"
            )
        tasks_block = "\n".join(lines)

        try:
            ends_dt = datetime.strptime(wave["ends_at"], "%Y-%m-%d %H:%M:%S")
            ends_txt = ends_dt.strftime("%d.%m")
        except (TypeError, ValueError, KeyError):
            ends_txt = str(wave.get("ends_at") or "")

        # D-11: вводный текст волны необязателен — {intro} подставляет пустую строку, а не
        # обрубок/висящее двоеточие; дефолт шаблона несёт перевод строки ПЕРЕД {intro}, так
        # что пустая подстановка оставляет лишнюю пустую строку — схлопываем её здесь, а не
        # правкой дефолта реестра (менеджер волен переписать шаблон по-своему).
        # WR-09: `intro_text` в БД — уже готовый HTML (`message.html_text` на записи), НЕ
        # сырой текст — повторный `html.escape` здесь превращал форматирование менеджера в
        # буквальные `&amp;`/`<b>`.
        intro = (wave.get("intro_text") or "").strip()
        intro_block = intro

        # CR-05: подстановка в текст, который правит менеджер, — цепочкой `.replace`
        # (`game_labels.fill_template`), НЕ `.format()`: посторонний `{`/`}` в тексте
        # менеджера не должен ронять рассылку целиком (T-073-03-05).
        template = i18n.tr(await get_setting_typed("wave_start_message_text"), lang, tr_map)
        text = game_labels.fill_template(
            template, wave=wave["number"], ends=ends_txt, intro=intro_block, tasks=tasks_block,
        )
        if not intro:
            text = text.replace("\n\n\n", "\n\n")

        button_text = i18n.tr(await get_setting_typed("wave_start_button_text"), lang, tr_map)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            # Переиспользуем существующий делегатский экран списка заданий (page 0) —
            # handlers/user_actions.py::F.data.startswith("gtasks_page:") — вместо нового
            # обработчика: список заданий уже есть, отдельного «экрана волны» план не заводит.
            InlineKeyboardButton(text=button_text, callback_data="gtasks_page:0"),
        ]])

        now = _now_moscow_naive()

        async def _sender():
            await _safe_send(
                lambda cid: _bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup),
                ambassador_id,
            )

        await quiet_hours.send_or_queue_text(
            now, ambassador_id, text, sender=_sender, parse_mode="HTML", reply_markup=markup,
        )
    except Exception as e:
        logger.error(f"send_wave_start_dm({wave_id}, {ambassador_id}) failed: {e}")


# ── Phase 32 (32-08, D-26): напоминание за сутки до дедлайна задания ─────────────────────

def schedule_task_deadline_reminder(task_id: int, deadline: datetime) -> bool:
    """Разовая джоба напоминания за сутки до дедлайна ОДНОГО задания, на `deadline - 24h`.
    Момент уже в прошлом — напоминать поздно, джоба НЕ ставится вовсе (не «догоняющая»
    отправка задним числом, в отличие от рассылки старта волны). Возвращает признак,
    поставлена ли она — `reconcile_wave_jobs` использует его, чтобы отличить «уже стоит» от
    «дедлайн слишком близко, реального пропуска нет»."""
    run_at = deadline - timedelta(hours=24)
    if run_at <= _now_moscow_naive():
        return False
    get_scheduler().add_job(
        send_task_deadline_reminder, "date", run_date=run_at, args=[task_id],
        id=f"task_deadline_reminder_{task_id}", replace_existing=True,
    )
    return True


def cancel_task_deadline_reminder(task_id: int) -> None:
    """Fail-soft снятие по тому же id — уже сработала или не стояла вовсе, оба случая ОК."""
    try:
        get_scheduler().remove_job(f"task_deadline_reminder_{task_id}")
    except Exception:
        pass


async def _task_out_of_wave_recipients(task: dict) -> list[int]:
    """Круг получателей задания ВНЕ волн (D-26, task.wave_id пуст): `audience == 'ambassadors'`
    — амбассадоры города задания, иначе — все делегаты этого города (`event_city` пуст —
    все города, без фильтра, тот же смысл, что и везде в проекте). Собственного аксессора в
    `database/db.py` для «все делегаты одного города» нет — этот план не имеет права трогать
    этот файл (вне `files_modified`), поэтому фильтрация — здесь, по уже существующим
    `list_ambassadors`/`get_all_users_dicts`."""
    from database.db import list_ambassadors, get_all_users_dicts
    import cities

    if task.get("audience") == "ambassadors":
        rows = await list_ambassadors(city_scope=cities.city_scope(task.get("event_city")))
        return [int(r["telegram_id"]) for r in rows]
    city = task.get("event_city")
    users = await get_all_users_dicts()
    if not city:
        return [int(u["telegram_id"]) for u in users]
    # WR-03 (32-REVIEW.md), тот же хвост в задании вне волн: «пусто/неизвестно = дефолтный
    # город» — везде в проекте (`cities.normalize_city`), а не только там, где город явно
    # заполнен. Сырое `== city` теряло делегатов дефолтного города с `event_city IS NULL`
    # (легаси-строки, импорт прошлого сезона) — они видят задание в списке (та же
    # `visible_tasks_for`/`list_active_tasks(city_scope=...)` нормализация), но напоминание о
    # дедлайне до них не доходило. Нормализация включается только при включённом модуле
    # городов — при выключенном `event_city` у задания в принципе не выставляется.
    if await cities.cities_module_on():
        target = cities.normalize_city(city)
        return [
            int(u["telegram_id"]) for u in users
            if cities.normalize_city(u.get("event_city")) == target
        ]
    return [int(u["telegram_id"]) for u in users if u.get("event_city") == city]


async def send_task_deadline_reminder(task_id: int) -> None:
    """Date-job target: напоминание за сутки до дедлайна ОДНОГО задания, только тем, кто ещё
    НЕ сдал (D-26 — `get_active_submission` не видит отклонённые сдачи, поэтому отклонённая
    сдача тоже считается «не сдал», делегат может пересдать). Перечитывает задание ДО
    формирования круга получателей: архивное или без срока — выход; срок сдвинули так, что
    до него снова больше суток — переставляем джобу на новый момент (та же id, `replace_
    existing=True` не удваивает) и выходим, не рассылая рано."""
    try:
        from database.db import get_task, get_active_submission, list_ambassadors, get_wave, task_title
        from services.ambassador_waves import wave_eligible
        from services import quiet_hours, i18n
        import game_labels

        task = await get_task(task_id)
        if not task or task.get("archived_at"):
            return
        deadline = game_labels.task_deadline(task)
        if deadline is None:
            return
        now = _now_moscow_naive()
        if deadline - timedelta(hours=24) > now:
            schedule_task_deadline_reminder(task_id, deadline)
            return
        if deadline <= now:
            # Дедлайн сдвинули РАНЬШЕ уже после постановки этой джобы — «скоро дедлайн» после
            # самого дедлайна вводит в заблуждение (WR-13в), не переставляем на прошедший
            # момент, просто не шлём.
            return

        wave_id = task.get("wave_id")
        if wave_id:
            wave = await get_wave(int(wave_id))
            if not wave:
                return
            import cities
            recipients = []
            for a in await list_ambassadors(city_scope=cities.city_scope(wave.get("event_city"))):
                user = dict(a)
                user["is_ambassador"] = 1
                if wave_eligible(user, wave):
                    recipients.append(int(a["telegram_id"]))
        else:
            recipients = await _task_out_of_wave_recipients(task)

        template_raw = await get_setting_typed("wave_deadline_reminder_text")
        title = task_title(task)
        deadline_txt = await game_labels.task_deadline_text(task)

        for uid in recipients:
            # CR-05: сбой у ОДНОГО получателя (например, кривой src в i18n) не должен
            # обрывать напоминание остальным несдавшим — тот же приём, что в `send_wave_results`.
            try:
                if await get_active_submission(task_id, uid):
                    continue  # уже сдал (сдача не отклонена) — D-26: только несдавшим
                lang, tr_map = await i18n.context(uid)
                template = i18n.tr(template_raw, lang, tr_map)
                text = game_labels.fill_template(
                    template, task=html.escape(str(title)), coins=int(task["coins"]),
                    deadline=deadline_txt,
                )

                async def _sender(cid=uid, txt=text):
                    await _safe_send(lambda c: _bot.send_message(c, txt, parse_mode="HTML"), cid)

                await quiet_hours.send_or_queue_text(now, uid, text, sender=_sender, parse_mode="HTML")
                await asyncio.sleep(0.05)  # та же пауза между отправками, что в broadcast_run.py
            except Exception as e:
                logger.error(f"send_task_deadline_reminder({task_id}) recipient {uid} failed: {e}")
    except Exception as e:
        logger.error(f"send_task_deadline_reminder({task_id}) failed: {e}")


# ── Phase 32 (32-08, D-16/D-30): сводка менеджеру в конце волны ──────────────────────────

def schedule_wave_end(wave_id: int, ends_at: datetime) -> None:
    """Разовая джоба `f"wave_end_{wave_id}"` на момент конца волны. `replace_existing=True` —
    сдвиг даты волны просто перезаписывает джобу, без ручной отмены со стороны вызывающего.

    WR-05: `ends_at` в прошлом (бот лежал дольше `_MISFIRE_GRACE_SECONDS`, `jobs.sqlite`
    пересоздан, или волну завели/запустили задним числом) — та же ловушка и тот же приём, что
    уже применяется к старту волны (`schedule_wave_start_for_all`): «сейчас + минута», а не
    просроченный `run_date`, который APScheduler тихо выбрасывает как misfire — без этого
    волна оставалась `active` навсегда, а объявить итоги было неоткуда (карточка `active`-волны
    кнопки «Итоги» не показывает)."""
    now = _now_moscow_naive()
    run_at = ends_at if ends_at > now else now + timedelta(minutes=1)
    get_scheduler().add_job(
        send_wave_end_ping, "date", run_date=run_at, args=[wave_id],
        id=f"wave_end_{wave_id}", replace_existing=True,
    )


async def send_wave_end_ping(wave_id: int) -> None:
    """Date-job target: сводка менеджерам о конце волны (D-16/D-30), ровно один раз.
    `close_wave` — атомарный переход `active -> closing`; `False` (волна уже не 'active' —
    повторное срабатывание после переармирования, или менеджер уже объявил итоги руками) —
    молча выходим, ни одного сообщения (T-32-08-03).

    Текстовая обёртка над капабилити в `handlers/admin_caps.py` не поддерживает `reply_markup`
    (текст и кнопки шли бы раздельно), поэтому здесь используется публичный примитив резолва
    получателей `capability_holders(cap, city=...)` напрямую — тот же city-скоуп и тот же
    fallback на `config.ADMIN_IDS`, если у capability вовсе нет держателей (T-32-08-02),
    плюс `_safe_send` на отправку одним сообщением с текстом и клавиатурой вместе."""
    try:
        import game_labels
        from services.ambassador_waves import close_wave, wave_end_summary
        from handlers.admin_caps import capability_holders

        if not await close_wave(wave_id):
            return
        summary = await wave_end_summary(wave_id)
        wave = summary["wave"] or {}
        city = wave.get("event_city")

        top_lines = [
            f"{row['place']}. {html.escape(str(row.get('name') or row['user_id']))} — {row['points']}"
            for row in summary["top"]
        ]
        top_txt = "; ".join(top_lines) if top_lines else "пока пусто"

        # CR-05: `.replace`-подстановка (fill_template), не `.format()` — менеджерский текст
        # может содержать посторонний `{`/`}`.
        template = await get_setting_typed("wave_end_manager_text")
        text = game_labels.fill_template(
            template, wave=wave.get("number"), top=top_txt, pending=summary["pending"],
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏁 Объявить итоги", callback_data=f"wavefin:{wave_id}")],
            [InlineKeyboardButton(text="📋 Открыть сдачи на проверке", callback_data="admin_game_review")],
        ])

        recipients = await capability_holders("moderate_game", city=city)
        if not recipients:
            recipients = list(config.ADMIN_IDS)
        for uid in recipients:
            await _safe_send(
                lambda cid: _bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup), uid,
            )
    except Exception as e:
        logger.error(f"send_wave_end_ping({wave_id}) failed: {e}")


# ── Phase 32 (32-11, D-16/D-17): рассылка итогов волны из неизменяемого снимка ───────────
# CR-06: джоба принимает ТОЛЬКО `wave_id` (как все остальные джобы этого файла) и читает ВЕСЬ
# снимок из БД (`database.db.get_wave_results(wave_id, winners_only=False)`) — до этой правки
# личные места/баллы всех НЕ-призёров жили ТОЛЬКО в JSON-аргументе джобы: потеря джобы
# (`jobs.sqlite` пересоздан, простой дольше `_MISFIRE_GRACE_SECONDS`) или падение посреди
# рассылки теряло их навсегда — пересчитать из `wave_rating` задним числом D-17 запрещает.
# Персональная отметка `notified_at` на каждой строке снимка делает рассылку идемпотентной и
# возобновляемой: повторный запуск (после сбоя, после `reconcile_wave_jobs`) шлёт только тем,
# кому ещё не ушло, не задваивая уже получивших.

def schedule_wave_results_broadcast(wave_id: int) -> None:
    """Разовая джоба рассылки итогов ОДНОЙ волны, id `wave_results_broadcast_{wave_id}`,
    `replace_existing=True`. «Сейчас + минута», не «прямо сейчас внутри хендлера»: рассылка
    сотням участников не должна выполняться внутри обработчика кнопки менеджера (T-32-11-06) и
    обязана пережить рестарт бота — тот же приём, что `schedule_payment_reminder`/`schedule_
    wave_start`."""
    run_at = _now_moscow_naive() + timedelta(minutes=1)
    get_scheduler().add_job(
        send_wave_results, "date", run_date=run_at, args=[wave_id],
        id=f"wave_results_broadcast_{wave_id}", replace_existing=True,
    )


async def send_wave_results(wave_id: int) -> None:
    """Date-job target: рассылка итогов волны (D-16/D-17). Идемпотентна и возобновляема: волна
    не в состоянии 'announced' (откатили руками) — молча выходим; каждая строка снимка с уже
    проставленным `notified_at` пропускается (CR-06) — повторное срабатывание/переармирование
    досылает только хвост, не задваивая доставленное.

    Снимок — ЕДИНСТВЕННЫЙ источник личных мест/баллов (`database.db.get_wave_results(wave_id,
    winners_only=False)`), не пересчитанный на лету рейтинг волны (сдача, одобренная уже ПОСЛЕ
    объявления, не имеет права задним числом поменять то, что участник увидит в сообщении об
    итогах — T-32-11-01). Каждому неотправленному участнику уходит `wave_results_announce_text`;
    призёрам (`is_winner` в снимке) ДОПОЛНИТЕЛЬНО — `wave_results_winner_text` с текстом приза
    `wave_results_prize_text` (D-19: сам приз бот не выдаёт, это только текст). Оба сообщения —
    через `services.i18n.context` (язык участника), `quiet_hours.send_or_queue_text` и
    `_safe_send` (внутри `sender`), с паузой между получателями; свой `try/except` НА КАЖДОГО
    получателя (CR-05) — сбой одного (кривой src в i18n, блокировка бота) не обрывает рассылку
    остальным."""
    try:
        from database.db import get_wave, get_wave_results, get_display_names, mark_wave_result_notified
        import game_labels
        from services import quiet_hours, i18n

        wave = await get_wave(wave_id)
        if not wave or wave.get("state") != "announced":
            return

        snapshot = await get_wave_results(wave_id, winners_only=False)  # уже ORDER BY place ASC
        if not snapshot:
            return
        total = len(snapshot)

        winners = [r for r in snapshot if r["is_winner"]]
        winner_ids = {int(w["user_id"]) for w in winners}
        winner_names = await get_display_names([int(w["user_id"]) for w in winners])
        winners_txt = "; ".join(
            f"{w['place']}. {html.escape(str(winner_names.get(int(w['user_id']), w['user_id'])))}"
            for w in winners
        ) or "—"

        announce_raw = await get_setting_typed("wave_results_announce_text")
        winner_raw = await get_setting_typed("wave_results_winner_text")
        prize_raw = await get_setting_typed("wave_results_prize_text")
        wave_number = wave.get("number")
        now = _now_moscow_naive()

        for row in snapshot:
            if row.get("notified_at"):
                continue  # этому участнику итоги уже ушли — не задваиваем (CR-06)
            user_id = int(row["user_id"])
            place = int(row["place"])
            points = int(row["points"])
            try:
                if not await mark_wave_result_notified(
                    wave_id, user_id, now.strftime("%Y-%m-%d %H:%M:%S"),
                ):
                    continue  # отметил параллельный тик той же джобы — не шлём вторично

                lang, tr_map = await i18n.context(user_id)
                text = game_labels.fill_template(
                    i18n.tr(announce_raw, lang, tr_map),
                    wave=wave_number, winners=winners_txt, place=place, total=total, points=points,
                )

                async def _sender(cid=user_id, txt=text):
                    await _safe_send(lambda c: _bot.send_message(c, txt, parse_mode="HTML"), cid)

                await quiet_hours.send_or_queue_text(now, user_id, text, sender=_sender, parse_mode="HTML")
                await asyncio.sleep(0.05)  # та же пауза между отправками, что в broadcast_run.py

                if user_id not in winner_ids:
                    continue
                prize_text = i18n.tr(prize_raw, lang, tr_map)
                winner_text = game_labels.fill_template(
                    i18n.tr(winner_raw, lang, tr_map),
                    wave=wave_number, place=place, points=points, prize=prize_text,
                )

                async def _winner_sender(cid=user_id, txt=winner_text):
                    await _safe_send(lambda c: _bot.send_message(c, txt, parse_mode="HTML"), cid)

                await quiet_hours.send_or_queue_text(
                    now, user_id, winner_text, sender=_winner_sender, parse_mode="HTML",
                )
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"send_wave_results({wave_id}) recipient {user_id} failed: {e}")
    except Exception as e:
        logger.error(f"send_wave_results({wave_id}) failed: {e}")


# ── Phase 32 (32-08, T-32-08-07): переармирование джоб волн на старте бота ───────────────

async def reconcile_wave_jobs() -> None:
    """Вызывается из `init_scheduler` после существующих реконсиляций. По АКТИВНЫМ волнам
    заново ставит недостающие джобы: стартовую рассылку (только для волн, у которых
    `started_notified_at` пуст — уже стартовавшая волна не рассылается заново),
    напоминания по заданиям волны (только для заданий с будущим сроком — прошедшее
    напоминание не воскрешается) и джобу конца волны. Идемпотентно: все идентификаторы
    детерминированные, `replace_existing=True` не плодит дублей — повторный вызов на уже
    полностью взведённом хранилище — no-op по факту (джобы просто перезаписываются теми же
    значениями).

    CR-06: по волнам `announced` с неразосланными строками снимка (`notified_at IS NULL` хотя
    бы у одной) заново ставит джобу рассылки итогов — потерянная джоба (пересозданный
    `jobs.sqlite`, простой дольше `_MISFIRE_GRACE_SECONDS`, необработанное исключение в старой
    версии рассылки) лечится обычным рестартом бота, без ручного SQL и без повторной отправки
    уже уведомлённым (`send_wave_results` сама пропускает строки с проставленным `notified_at`).

    WR-13(б) (32-FIX-common-2): выше переармируются только задания волн из `states=("active",)`
    — задание ВНЕ волн (`wave_id` пуст) со своим сроком раньше вообще не переармировалось,
    хотя напоминание о нём ставит тот же `schedule_task_deadline_reminder` (визард бота при
    создании — `handlers/admin_game_waves.py`, а любую правку дедлайна из Mini App лечит
    отдельная точка `task_changed`). Ниже — второй проход по всем активным (не архивным)
    заданиям без волны; идемпотентно (та же `replace_existing=True`), прошедшее напоминание не
    воскрешается — `schedule_task_deadline_reminder` сама возвращает `False` на прошедший
    момент и джобу не ставит."""
    try:
        from database.db import (
            list_active_tasks, list_waves, list_wave_tasks, count_wave_results_pending_notify,
        )
        import game_labels

        waves = await list_waves(states=("active",))
        for wave in waves:
            wave_id = int(wave["id"])
            if not wave.get("started_notified_at"):
                await schedule_wave_start_for_all(wave_id)
            try:
                ends_dt = datetime.strptime(wave["ends_at"], "%Y-%m-%d %H:%M:%S")
                schedule_wave_end(wave_id, ends_dt)
            except (TypeError, ValueError, KeyError):
                pass

            tasks = await list_wave_tasks(wave_id, active_only=True)
            for t in tasks:
                deadline = game_labels.task_deadline(t)
                if deadline is not None:
                    schedule_task_deadline_reminder(int(t["id"]), deadline)

        for t in await list_active_tasks():
            if t.get("wave_id"):
                continue  # уже переармировано выше вместе со своей волной
            deadline = game_labels.task_deadline(t)
            if deadline is not None:
                schedule_task_deadline_reminder(int(t["id"]), deadline)

        for wave in await list_waves(states=("announced",)):
            wave_id = int(wave["id"])
            if await count_wave_results_pending_notify(wave_id) > 0:
                schedule_wave_results_broadcast(wave_id)
    except Exception as e:
        logger.error(f"reconcile_wave_jobs failed: {e}")
