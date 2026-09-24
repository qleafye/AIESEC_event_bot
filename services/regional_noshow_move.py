"""Трек «региональные форумы → Москва» (04.10): после последнего дня форума РЕГИОНАЛЬНОГО
города (СПб/Тюмень 03.10) одобренным делегатам текущего сезона, которых не видели ни на входе,
ни на сессиях форума, уходит предложение перенести заявку на московский форум (30-31.10).

Форма модуля byte-в-byte `services/forum_noshow_poll.py` — та же идемпотентность отправки
(`UNIQUE(telegram_id, season)`, `database.db.regional_noshow_move`), та же one-shot date-джоба на
город, та же очередь тихих часов, тот же fail-soft. Отличия:

  - аудитория ДОПОЛНИТЕЛЬНО минус те, кто в `forum_noshow_poll` ЭТОГО сезона выбрал причину
    «Передумал(а)» (`database.db.regional_noshow_move_pending_ids`) — предлагать Москву тому,
    кто прямо сказал «не интересно», незачем;
  - порядок с опросом неявившихся: если ОБА тумблера включены — предложение уходит НЕ РАНЬШЕ,
    чем через `_POLL_OFFSET_HOURS` часов после расчётного времени отправки опроса (`_run_at_for`
    ниже). Простое и детерминированное правило вместо ожидания «ответил ли делегат на опрос» —
    неотвеченный опрос иначе блокировал бы предложение бесконечно; расстояние в часы гарантирует
    ровно то, что требуется («не слать одновременно»), без обращения к состоянию опроса другого
    делегата на момент отправки;
  - ответ делегата не одна кнопка, а мини-флоу «предложение -> подтверждение -> перенос»
    (`handlers/user_actions.py::rnm_accept/rnm_confirm/rnm_decline`) — сам перенос делает
    `services/city_move.py::move_user_city` (Phase 33), эта строка только фиксирует факт
    (`response`/`target_city`) в своей таблице;
  - менеджеру города НАЗНАЧЕНИЯ уходит АГРЕГИРОВАННАЯ сводка, не сообщение на каждого
    переехавшего — интервальная джоба `_notify_managers_job` (раз в `_NOTIFY_INTERVAL_MINUTES`
    минут) сканирует ещё не отправленные строки `response=RNM_MOVED`, группирует по городу
    назначения и шлёт одно сообщение держателям `moderate_reg` (`handlers.admin_caps.
    notify_by_capability`, city-scoped)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from cities import default_city_code, get_setting_typed_for_city
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_JOB_PREFIX = "regional_noshow_move:"
_NOTIFY_JOB_ID = "regional_noshow_move:notify_managers"
_NOTIFY_INTERVAL_MINUTES = 30
_POLL_OFFSET_HOURS = 3  # см. докстринг модуля — расстояние от расчётного времени опроса

DEFAULT_TIME = "12:00"
DEFAULT_OFFER_TEXT = "Не получилось на форум в {city}? Приезжай на Юлид в Москве {dates}"

STATUS_MODE_KEEP = "keep"
STATUS_MODE_TO_MODERATION = "to_moderation"


def job_id(city: str | None) -> str:
    return f"{_JOB_PREFIX}{city or 'all'}"


# ── Резолверы настроек ──────────────────────────────────────────────────────────────────────

async def enabled_for(city: str | None) -> bool:
    return await get_setting_typed_for_city("regional_noshow_offer_enabled", city) == "on"


async def _time_for(city: str | None) -> str:
    return await get_setting_typed_for_city("regional_noshow_offer_time", city) or DEFAULT_TIME


async def target_city_for(city: str | None) -> str:
    """Город назначения — `regional_noshow_target_city` этого (регионального) города, дефолт —
    `cities.default_city_code()` (главный/московский город реестра, НИКОГДА не хардкод "msk")."""
    raw = await get_setting_typed_for_city("regional_noshow_target_city", city)
    return raw or default_city_code()


async def move_status_for(city: str | None) -> str:
    raw = await get_setting_typed_for_city("regional_noshow_move_status", city)
    return raw if raw == STATUS_MODE_TO_MODERATION else STATUS_MODE_KEEP


async def _offer_text_for(city: str | None) -> str:
    return await get_setting_typed_for_city("regional_noshow_offer_text", city) or DEFAULT_OFFER_TEXT


def _parse_hhmm(raw: str | None) -> tuple[int, int]:
    text = (raw or DEFAULT_TIME).strip()
    try:
        hh_s, mm_s = text.split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        return hh, mm
    except (ValueError, TypeError):
        hh_s, mm_s = DEFAULT_TIME.split(":")
        return int(hh_s), int(mm_s)


# ── Дата последнего дня форума (своя копия — тот же приём, что sos.py/forum_day_menu.py/
# forum_day_report.py/forum_noshow_poll.py/staff_expiry.py, ни один из них не переиспользует
# чужой приватный хелпер) ────────────────────────────────────────────────────────────────────

async def _last_forum_day(city: str | None) -> date | None:
    from services.reject_rules import forum_date_for  # ленивый импорт — цикл-разрыв, тот же
    # приём, что у соседних модулей.

    raw = await forum_date_for(city)
    if not raw:
        return None
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None

    from services.sos import DEFAULT_ACTIVE_DAYS  # публичная константа, sos.py не правим

    raw_days = await get_setting_typed_for_city("sos_active_days", city)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = DEFAULT_ACTIVE_DAYS
    if days < 1:
        days = 1
    return start + timedelta(days=days - 1)


async def _dates_label_for(target_city: str) -> str:
    """«30.10.2026–31.10.2026» (или один день, если форум города назначения однодневный) —
    подставляется в плейсхолдер `{dates}` ПОСЛЕ перевода текста (докстринг модуля, RULES.md)."""
    from services.reject_rules import forum_date_for

    raw = await forum_date_for(target_city)
    if not raw:
        return ""
    raw = raw.strip()
    last_day = await _last_forum_day(target_city)
    try:
        start = datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return raw
    if last_day is None or last_day == start:
        return start.strftime("%d.%m.%Y")
    return f"{start.strftime('%d.%m.%Y')}–{last_day.strftime('%d.%m.%Y')}"


# ── Планирование ─────────────────────────────────────────────────────────────────────────────

async def _run_at_for(city: str | None) -> tuple[datetime | None, str | None]:
    last_day = await _last_forum_day(city)
    if last_day is None:
        return None, "no_date"
    target_day = last_day + timedelta(days=1)

    hh, mm = _parse_hhmm(await _time_for(city))
    run_at = datetime.combine(target_day, time(hh, mm))

    from services.forum_noshow_poll import enabled_for as poll_enabled_for
    if await poll_enabled_for(city):
        poll_hh, poll_mm = _parse_hhmm(await get_setting_typed_for_city("forum_noshow_poll_time", city))
        poll_run_at = datetime.combine(target_day, time(poll_hh, poll_mm))
        min_run_at = poll_run_at + timedelta(hours=_POLL_OFFSET_HOURS)
        if min_run_at > run_at:
            run_at = min_run_at

    return run_at, None


async def schedule_city_job(city: str | None) -> dict:
    """(Пере)ставить/снять one-shot джобу этого города — вызывается и `reconcile()` (старт
    бота), и сразу после правки настройки (`handlers/admin_settings.py::
    _reschedule_regional_noshow_move_if_relevant`)."""
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    jid = job_id(city)

    if not await enabled_for(city):
        cancel_city_job(city)
        return {"scheduled": False, "reason": "disabled"}

    run_at, reason = await _run_at_for(city)
    if run_at is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": reason}

    now = msk_now()
    if now.date() > run_at.date() + timedelta(days=2):
        # Целевой день давно прошёл (бот был выключен несколько дней) — не досылаем предложение
        # спустя долгое время, тот же баланс, что у forum_noshow_poll.schedule_city_job.
        cancel_city_job(city)
        return {"scheduled": False, "reason": "past"}
    if run_at <= now:
        run_at = now + timedelta(minutes=1)  # догон — бот был выключен в момент отправки

    sched.add_job(
        _run_job, "date", run_date=run_at, args=[city], id=jid, replace_existing=True,
    )
    return {"scheduled": True, "run_at": run_at}


def cancel_city_job(city: str | None) -> None:
    from services.scheduler import get_scheduler

    try:
        get_scheduler().remove_job(job_id(city))
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК


async def _city_still_valid(city: str | None) -> bool:
    if city is not None:
        from cities import cities_module_on, enabled_cities
        if await cities_module_on():
            codes = {c["code"] for c in await enabled_cities()}
            if city not in codes:
                return False
    return await enabled_for(city)


async def _run_job(city: str | None) -> dict:
    if not await _city_still_valid(city):
        logger.info(f"regional_noshow_move: job for city={city!r} skipped — город/предложение выключены")
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}
    return await send_offers(city)


def _cancel_stale_city_jobs(enabled_codes: set[str]) -> None:
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    for job in sched.get_jobs():
        if not job.id.startswith(_JOB_PREFIX) or job.id == _NOTIFY_JOB_ID:
            continue
        code = job.id[len(_JOB_PREFIX):]
        if code != "all" and code not in enabled_codes:
            try:
                sched.remove_job(job.id)
            except Exception:
                pass


def _reconcile_notify_job() -> None:
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    sched.add_job(
        _notify_managers_job, "interval", minutes=_NOTIFY_INTERVAL_MINUTES,
        id=_NOTIFY_JOB_ID, replace_existing=True,
    )


async def reconcile() -> list[str | None]:
    from cities import cities_module_on, enabled_cities

    touched: list[str | None] = []
    try:
        if await cities_module_on():
            enabled_codes = {c["code"] for c in await enabled_cities()}
            codes = list(enabled_codes)
        else:
            enabled_codes = None
            codes = [None]
        for code in codes:
            try:
                await schedule_city_job(code)
                touched.append(code)
            except Exception as e:
                logger.error(f"regional_noshow_move.reconcile({code!r}) failed: {e}")
        if enabled_codes is not None:
            try:
                _cancel_stale_city_jobs(enabled_codes)
            except Exception as e:
                logger.error(f"regional_noshow_move.reconcile: stale sweep failed: {e}")
        try:
            _reconcile_notify_job()
        except Exception as e:
            logger.error(f"regional_noshow_move.reconcile: notify job failed: {e}")
    except Exception as e:
        logger.error(f"regional_noshow_move.reconcile failed: {e}")
    return touched


# ── Рассылка предложения ────────────────────────────────────────────────────────────────────

def offer_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Перенести мою заявку в Москву", callback_data="rnm_accept")],
        [InlineKeyboardButton(text="Нет, спасибо", callback_data="rnm_decline")],
    ])


async def send_offers(city: str | None) -> dict:
    """Отправляет предложение всем кандидатам региона (`city=None` — все города, модуль
    выключен). Троттлинг/мут/тихие часы — тот же приём, что `forum_noshow_poll.send_poll`."""
    from database.db import get_muted_today_ids, get_user, regional_noshow_move_mark_sent, regional_noshow_move_pending_ids
    from settings_schema import get_setting_typed
    from services import quiet_hours
    import cities as _cities

    bot = _bot()
    if bot is None:
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}

    season = (await get_setting_typed("event_season") or "").strip()
    scope = _cities.city_scope(city)
    targets = await regional_noshow_move_pending_ids(city_scope=scope)
    if not targets:
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}

    target_city = await target_city_for(city)
    raw_text = await _offer_text_for(city)
    dates_label = await _dates_label_for(target_city)
    now = msk_now()
    muted = await get_muted_today_ids(now.strftime("%Y-%m-%d"))
    kb = offer_keyboard()

    sent = queued = skipped_muted = failed = 0
    for tid in targets:
        if tid in muted:
            skipped_muted += 1
            continue
        user = await get_user(tid)
        if user is None:
            continue
        marked = await regional_noshow_move_mark_sent(
            tid, user.get("event_city"), season, now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not marked:
            continue

        from handlers import reg_i18n
        from services import i18n as i18n_service

        lang, tr_map = await i18n_service.context(tid)
        source_city_label = await _cities.city_label(_cities.normalize_city(user.get("event_city")))
        text = reg_i18n.tr_text(raw_text, lang, tr_map)
        # Плейсхолдеры подставляются ПОСЛЕ перевода (RULES.md) — иначе EN-делегат увидел бы
        # русское название города/даты в переведённом тексте.
        text = text.replace("{city}", source_city_label).replace("{dates}", dates_label)
        tr_kb = reg_i18n.tr_kb(kb, lang, tr_map)
        try:
            delivered_now = await quiet_hours.send_or_queue_text(
                now, tid, text,
                sender=lambda cid=tid, t=text, k=tr_kb: bot.send_message(cid, t, reply_markup=k),
                reply_markup=tr_kb,
            )
        except Exception as e:
            logger.warning(f"regional_noshow_move.send_offers: доставка {tid} упала: {e}")
            failed += 1
            continue
        if delivered_now:
            sent += 1
        else:
            queued += 1
        await asyncio.sleep(0.05)

    logger.info(
        f"regional_noshow_move.send_offers({city!r}): sent {sent}, queued {queued}, "
        f"muted {skipped_muted}, failed {failed} of {len(targets)}"
    )
    return {"sent": sent, "queued": queued, "muted": skipped_muted, "failed": failed, "total": len(targets)}


def _bot():
    try:
        import services.scheduler as scheduler_module
        return scheduler_module.get_bot()
    except Exception:
        return None


# ── Ответ делегата ───────────────────────────────────────────────────────────────────────────

async def get_state(telegram_id: int) -> dict | None:
    """Текущая строка предложения этого делегата в ТЕКУЩЕМ сезоне — `None`, если предложение
    не уходило вовсе (чужой/устаревший callback_data, вызывающий отвечает тихо)."""
    from database.db import regional_noshow_move_get
    from settings_schema import get_setting_typed

    season = (await get_setting_typed("event_season") or "").strip()
    return await regional_noshow_move_get(telegram_id, season)


async def record_decline(telegram_id: int) -> bool:
    from database.db import RNM_DECLINED, record_regional_noshow_move_response
    from settings_schema import get_setting_typed

    season = (await get_setting_typed("event_season") or "").strip()
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    return await record_regional_noshow_move_response(telegram_id, season, RNM_DECLINED, None, stamp)


async def apply_move(telegram_id: int, *, source_city: str | None) -> dict:
    """Выполняет сам перенос (`services.city_move.move_user_city`) и фиксирует ответ строкой
    `response=RNM_MOVED`. `by_admin=0` — системный маркер (перенос инициирован делегатом по
    кнопке, не менеджером); `move_user_city` принимает `by_admin: int`, но сегодня нигде не
    пишет его в БД (докстринг `services/city_move.py`) — маркер задокументирован здесь на
    случай, если функция начнёт его использовать."""
    from database.db import RNM_MOVED, record_regional_noshow_move_response
    from services.city_move import move_user_city
    from settings_schema import get_setting_typed

    target_city = await target_city_for(source_city)
    status_mode = await move_status_for(source_city)
    report = await move_user_city(
        telegram_id, target_city, status_mode=status_mode, by_admin=0, dry_run=False,
    )
    if report.get("ok"):
        season = (await get_setting_typed("event_season") or "").strip()
        stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
        await record_regional_noshow_move_response(telegram_id, season, RNM_MOVED, target_city, stamp)
    return report


async def summary_text(*, city_scope=None) -> str:
    """«Предложено N, перенеслись M, отказались K» — строка экрана менеджера (`handlers.
    admin_forum_functions`)."""
    from database.db import regional_noshow_move_summary
    from settings_schema import get_setting_typed

    season = (await get_setting_typed("event_season") or "").strip()
    s = await regional_noshow_move_summary(season, city_scope=city_scope)
    return f"Предложено {s['offered']}, перенеслись {s['moved']}, отказались {s['declined']}"


# ── Сводка менеджеру города назначения ──────────────────────────────────────────────────────

async def _notify_managers_job() -> None:
    from database.db import regional_noshow_move_mark_notified, regional_noshow_move_unnotified_moved
    from handlers.admin_caps import notify_by_capability
    import cities as _cities

    bot = _bot()
    if bot is None:
        return
    rows = await regional_noshow_move_unnotified_moved()
    if not rows:
        return

    by_target: dict[str | None, list[dict]] = {}
    for row in rows:
        by_target.setdefault(row.get("target_city"), []).append(row)

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    for target_city, target_rows in by_target.items():
        by_source: dict[str | None, int] = {}
        for row in target_rows:
            source = row.get("source_city")
            by_source[source] = by_source.get(source, 0) + 1
        parts = []
        for source, count in by_source.items():
            label = await _cities.city_label(_cities.normalize_city(source))
            parts.append(f"из {label} — {count}")
        total = len(target_rows)
        text = f"🚌 Перенеслись {total} делегат(ов) регионального форума: {', '.join(parts)}."
        try:
            await notify_by_capability(bot, "moderate_reg", text, city=target_city)
        except Exception as e:
            logger.error(f"regional_noshow_move._notify_managers_job: notify({target_city!r}) failed: {e}")
            continue
        await regional_noshow_move_mark_notified([r["id"] for r in target_rows], now)
