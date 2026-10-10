"""Идея №16 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): «📊 Отчёт дня
форума» вечером — в КАЖДЫЙ день форума города (окно дней форума, БЕЗ «вечера накануне» —
в отличие от `services/forum/forum_day_menu.py`, отчёт про сам день, а не про подготовку к нему) в
настраиваемое время (per_city, дефолт 21:00) бот шлёт отчёт: в привязанный чат SOS города
(та же привязка, что `services.sos.sos_chat_for_city` — читаем ЕЁ ПУБЛИЧНУЮ функцию, `sos.py`
не правим) и личным сообщением держателям capability `moderate_reg` этого города
(`handlers.access.admin_caps.capability_holders` — тот же примитив, что у соседних форумных рассылок,
уже умеет фоллбэк «никто не привязан к городу -> все держатели»).

Идемпотентность АВТОМАТИЧЕСКОЙ отправки — по (город, день форума), `database.db.
forum_day_report_sends`. Ручная кнопка «📊 Отчёт дня сейчас» (`handlers/forum/admin_forum_functions.py`)
эту таблицу НЕ трогает вовсе — зовёт `build_report_text`/`send_report(..., mark_sent=False)`
напрямую, чтобы повторный ручной запуск не путался с автоматической идемпотентностью и не гасил
вечернюю джобу того же дня.

Планирование — ОДНА self-rescheduling джоба на город (тот же приём, что
`services/checkin_volunteer_broadcast.py`): `schedule_city_job` находит СЛЕДУЮЩИЙ ещё не
отправленный день окна форума и ставит джобу РОВНО на него; после срабатывания джоба сама
переставляет себя на следующий день (`_run_job` зовёт `schedule_city_job` в хвосте). Окно дней
форума — `[forum_date, forum_date + sos_active_days - 1]`, тот же расчёт, что `services.
forum_day_menu._forum_window_dates`/`services.sos.is_sos_active_for_city` (дублируем чтение тех
же двух ключей реестра, `sos.py`/`forum_day_menu.py` не правим).

Тексты отчёта — ДЛЯ МЕНЕДЖЕРОВ (владелец, задание): русские литералы в коде, без реестра и без
перевода — тот же приём, что у остальных отчётов персоналу (`services/daily_digest.py`,
`services/checkin_volunteer_broadcast.py` шлёт РЕГИСТРОВЫЙ, но тоже НЕпереводимый текст
персоналу, group "apps"). Каждая строка отчёта — ТОЛЬКО если по ней реально есть данные (нет
подходящих строк в БД -> строка отчёта пропускается целиком, включая заголовок раздела)."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, time, timedelta

from domain.cities import get_setting_typed_for_city
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

_JOB_PREFIX = "forum_day_report:"
DEFAULT_TIME = "21:00"


def job_id(city: str | None) -> str:
    return f"{_JOB_PREFIX}{city or 'all'}"


# ── Окно дней форума — дубль расчёта forum_day_menu._forum_window_dates/sos.is_sos_active_for_city ──

async def _window_dates(city: str | None) -> tuple[date, date] | None:
    from services.reject_rules import forum_date_for  # ленивый импорт — цикл-разрыв, тот же
    # приём, что services/sos.py/services/forum_day_menu.py.

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
    return start, start + timedelta(days=days - 1)


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


async def enabled_for(city: str | None) -> bool:
    return await get_setting_typed_for_city("forum_day_report_enabled", city) == "on"


async def _time_for(city: str | None) -> str:
    return await get_setting_typed_for_city("forum_day_report_time", city) or DEFAULT_TIME


async def _next_pending_day(city: str | None, start: date, end: date) -> date | None:
    from database.db import forum_day_report_sent_days

    sent = await forum_day_report_sent_days(city)
    d = start
    while d <= end:
        if d.strftime("%Y-%m-%d") not in sent:
            return d
        d += timedelta(days=1)
    return None


# ── Планирование — self-rescheduling джоба на город (форма services.checkin_volunteer_broadcast) ──

# Сбой джобы (база занята, сеть) — следующая попытка не раньше чем через полчаса, а не каждую
# минуту.
_RETRY_AFTER_FAILURE = timedelta(minutes=30)


async def schedule_city_job(city: str | None, *, after_failure: bool = False) -> dict:
    """(Пере)ставить джобу СЛЕДУЮЩЕГО ещё не отправленного дня форума этого города — или снять
    её (тумблер выключен / дата форума не задана / все дни окна уже отправлены / окно форума
    прошло с большим запасом). Вызывается и `reconcile()` (старт бота), и СРАЗУ после правки
    настройки (`handlers/settings/admin_settings.py::_reschedule_forum_day_report_if_relevant`)."""
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    jid = job_id(city)

    if not await enabled_for(city):
        cancel_city_job(city)
        return {"scheduled": False, "reason": "disabled"}

    window = await _window_dates(city)
    if window is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": "no_date"}
    start, end = window

    now = msk_now()
    if now.date() > end + timedelta(days=1):
        # Окно форума прошло больше суток назад — поздний рестарт не должен внезапно
        # присылать «отчёт дня» о форуме недельной давности (тот же баланс, что у
        # session_feedback._CATCHUP_GRACE_HOURS, только в днях — отчёт дня грубее по кванту).
        cancel_city_job(city)
        return {"scheduled": False, "reason": "past"}

    day = await _next_pending_day(city, start, end)
    if day is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": "all_sent"}

    hh, mm = _parse_hhmm(await _time_for(city))
    run_at = datetime.combine(day, time(hh, mm))
    if run_at <= now:
        # догон — бот был выключен в момент отправки; после сбоя — с паузой, и сверка раз в
        # 10 минут эту паузу не сокращает (уже стоящий догон в будущем не трогаем).
        existing = sched.get_job(jid)
        pending_at = getattr(existing, "next_run_time", None) if existing is not None else None
        if pending_at is not None and pending_at.replace(tzinfo=None) > now and not after_failure:
            return {"scheduled": True, "run_at": pending_at, "day": day.strftime("%Y-%m-%d")}
        run_at = now + (_RETRY_AFTER_FAILURE if after_failure else timedelta(minutes=1))

    sched.add_job(
        _run_job, "date", run_date=run_at, args=[city], id=jid, replace_existing=True,
    )
    return {"scheduled": True, "run_at": run_at, "day": day.strftime("%Y-%m-%d")}


def cancel_city_job(city: str | None) -> None:
    from services.scheduler import get_scheduler

    try:
        get_scheduler().remove_job(job_id(city))
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК


async def _city_still_valid(city: str | None) -> bool:
    if city is not None:
        from domain.cities import cities_module_on, enabled_cities
        if await cities_module_on():
            codes = {c["code"] for c in await enabled_cities()}
            if city not in codes:
                return False
    return await enabled_for(city)


async def _run_job(city: str | None) -> None:
    failed = False
    try:
        if not await _city_still_valid(city):
            logger.info(f"forum_day_report: job for city={city!r} skipped — город/отчёт выключены")
            return
        window = await _window_dates(city)
        if window is None:
            return
        start, end = window
        day = await _next_pending_day(city, start, end)
        if day is not None:
            await send_report(city, day.strftime("%Y-%m-%d"), mark_sent=True)
    except Exception as e:
        failed = True
        logger.error(f"forum_day_report._run_job({city!r}) failed: {e}")
    finally:
        try:
            # переставить на следующий ещё не отправленный день
            await schedule_city_job(city, after_failure=failed)
        except Exception as e:
            logger.error(f"forum_day_report._run_job({city!r}): reschedule failed: {e}")


def _cancel_stale_city_jobs(enabled_codes: set[str]) -> None:
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    for job in sched.get_jobs():
        if not job.id.startswith(_JOB_PREFIX):
            continue
        code = job.id[len(_JOB_PREFIX):]
        if code != "all" and code not in enabled_codes:
            try:
                sched.remove_job(job.id)
            except Exception:
                pass


async def reconcile() -> list[str | None]:
    """На боте: (пере)ставить джобу КАЖДОГО включённого города (или один общий проход
    `city=None`, если модуль городов выключен) — fail-soft НА ГОРОД, тот же приём, что
    `services.checkin_volunteer_broadcast.reconcile`."""
    from domain.cities import cities_module_on, enabled_cities

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
                logger.error(f"forum_day_report.reconcile({code!r}) failed: {e}")
        if enabled_codes is not None:
            try:
                _cancel_stale_city_jobs(enabled_codes)
            except Exception as e:
                logger.error(f"forum_day_report.reconcile: stale sweep failed: {e}")
    except Exception as e:
        logger.error(f"forum_day_report.reconcile failed: {e}")
    return touched


# ── Сборка текста отчёта — только строки, по которым реально есть данные ─────────────────────

_TG_LIMIT = 4096


def _hour_label(hh: str) -> str:
    return f"{hh}:00–{hh}:59"


async def build_report_text(city: str | None, day: str) -> str:
    from domain.cities import city_label, cities_module_on
    from database.db import (
        CHECKIN_ENTRY_POINT, checkin_not_arrived_summary, checkin_peak_hour_for_city_day,
        count_approved_current_season, count_checkins_by_point_and_day, sos_day_stats,
    )
    import domain.cities as _cities

    scope = _cities.city_scope(city)
    label = await city_label(city) if (city and await cities_module_on()) else None

    lines = ["📊 <b>Отчёт дня форума</b>" + (f" — {html.escape(label)}" if label else "") + f" ({day})"]

    arrived = await count_checkins_by_point_and_day(CHECKIN_ENTRY_POINT, day, city_scope=scope)
    approved = await count_approved_current_season(city_scope=scope)
    lines.append(f"👥 Пришли {arrived} из {approved} одобренных делегатов")

    peak = await checkin_peak_hour_for_city_day(day, city_scope=scope)
    if peak is not None:
        hh, n = peak
        # Метки в базе — МСК; час пика показываем по часам города (Тюмень МСК+2: 08 -> 10).
        from services.infra.timeutil import city_offset_hours
        offset = await city_offset_hours(city)
        if offset and str(hh).isdigit():
            hh = f"{(int(hh) + offset) % 24:02d}"
        lines.append(f"⏱ Пик прихода: {_hour_label(hh)} ({n} отметок)")

    try:
        from services.session_feedback import day_stats as _session_day_stats
        rows = await _session_day_stats(city, day) if city else []
    except Exception as e:
        logger.error(f"forum_day_report.build_report_text: session day_stats failed: {e}")
        rows = []

    attended = [r for r in rows if r.get("marked_count", 0) > 0]
    if attended:
        by_attendance = sorted(attended, key=lambda r: -r["marked_count"])
        top = by_attendance[:3]
        bottom = list(reversed(by_attendance[-3:])) if len(by_attendance) > 3 else []
        lines.append("🏆 Топ-3 сессии по посещаемости:")
        for r in top:
            lines.append(f"  • {html.escape(r['title'] or '')} — {r['marked_count']}")
        if bottom:
            lines.append("📉 Анти-топ-3 сессии по посещаемости:")
            for r in bottom:
                lines.append(f"  • {html.escape(r['title'] or '')} — {r['marked_count']}")

    rated = [r for r in rows if r.get("stats", {}).get("rating_count", 0) > 0]
    if rated:
        by_rating = sorted(rated, key=lambda r: -(r["stats"]["avg"] or 0))
        top = by_rating[:3]
        bottom = list(reversed(by_rating[-3:])) if len(by_rating) > 3 else []
        lines.append("⭐ Топ-3 сессии по оценке:")
        for r in top:
            lines.append(f"  • {html.escape(r['title'] or '')} — {r['stats']['avg']:.1f}")
        if bottom:
            lines.append("💔 Анти-топ-3 сессии по оценке:")
            for r in bottom:
                lines.append(f"  • {html.escape(r['title'] or '')} — {r['stats']['avg']:.1f}")

    cna = await checkin_not_arrived_summary(city_scope=scope, day=day)
    if cna["total"] > 0:
        lines.append(
            f"❓ Ответы на «не пришёл»: едут {cna['coming']} · не смогут {cna['cant']} · "
            f"уже на месте {cna['here']} · без ответа {cna['no_response']}"
        )

    sos = await sos_day_stats(day, city_scope=scope)
    if sos["total"] > 0:
        sos_line = f"🆘 SOS за день: {sos['total']} (решено {sos['resolved']})"
        if sos["avg_claim_minutes"] is not None:
            sos_line += f", среднее время до «Беру»: {sos['avg_claim_minutes']:.0f} мин"
        lines.append(sos_line)

    # Повторные сканы на входе (идея №10 бэклога чек-ина) — НЕ пишутся ни в одну таблицу
    # (record_checkin у точки "entry" — INSERT OR IGNORE, повтор просто отбрасывается без
    # следа), поэтому строки отчёта по ним нет — данных для неё в принципе не существует.

    text = "\n".join(lines)
    # Лимит Telegram — 4096 символов: строк немного (топ-3/анти-3), но названия сессий
    # произвольной длины — режем по границе строки, чтобы не порвать HTML-тег.
    if len(text) > _TG_LIMIT:
        cut = text.rfind("\n", 0, _TG_LIMIT - 2)
        text = text[: cut if cut > 0 else _TG_LIMIT - 2] + "\n…"
    return text


async def send_report(city: str | None, day: str, *, mark_sent: bool) -> dict:
    """Отправляет отчёт `day` города `city` в привязанный чат SOS + личным сообщением
    держателям `moderate_reg`. `mark_sent=True` — только у АВТОМАТИЧЕСКОЙ вечерней джобы
    (`_run_job`); ручная кнопка «Отчёт дня сейчас» зовёт с `mark_sent=False` (докстринг
    модуля)."""
    from database.db import forum_day_report_mark_sent
    from handlers.access.admin_caps import capability_holders
    from services.sos import sos_chat_for_city  # публичная функция, sos.py не правим

    text = await build_report_text(city, day)
    # Отметка ДО отправки (claim): раньше она шла после, и если запись падала (база занята),
    # джоба догоняла «через минуту» и слала тот же отчёт в чат SOS каждую минуту.
    if mark_sent and not await forum_day_report_mark_sent(
        city, day, msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    ):
        return {"chat_delivered": False, "dm_delivered": 0, "text": text, "already_sent": True}

    chat_delivered = False
    dm_delivered = 0
    bot = _bot()
    if bot is not None:
        chat = await sos_chat_for_city(city)
        if chat is not None:
            try:
                await bot.send_message(chat["chat_id"], text, parse_mode="HTML")
                chat_delivered = True
            except Exception as e:
                logger.warning(f"forum_day_report.send_report: чат id={chat['chat_id']} упал: {e}")

        recipients = await capability_holders("moderate_reg", city=city)
        for uid in recipients:
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                dm_delivered += 1
            except Exception as e:
                logger.info(f"forum_day_report.send_report: не удалось написать id={uid}: {e}")
            await asyncio.sleep(0.05)

    return {"chat_delivered": chat_delivered, "dm_delivered": dm_delivered, "text": text}


def _bot():
    try:
        import services.scheduler as scheduler_module
        return scheduler_module.get_bot()
    except Exception:
        return None


async def checkins_csv_for_city_day(city: str | None, day: str) -> bytes:
    """CSV отметок города за `day` — «📥 Выгрузить отметки (CSV)» кнопка отчёта дня
    (`handlers/forum/admin_forum_functions.py`)."""
    import csv
    import io

    from database.db import list_checkins_for_city_day
    import domain.cities as _cities

    rows = await list_checkins_for_city_day(day, city_scope=_cities.city_scope(city))
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["telegram_id", "full_name", "username", "point", "scanned_at", "source", "approx_time", "by_staff_id"])
    for r in rows:
        writer.writerow([
            r["telegram_id"], r.get("full_name") or "", r.get("username") or "",
            r["point"], r["scanned_at"], r["source"], r.get("approx_time") or 0,
            r.get("by_staff_id") or "",
        ])
    return output.getvalue().encode("utf-8-sig")
