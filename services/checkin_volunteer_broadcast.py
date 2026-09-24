"""D-33 (решение владельца 24.09, `.planning/FORUM-CHECKIN.md`): шпаргалка волонтёра чек-ина
(`checkin_volunteer_guide_text` — тот же текст, что `handlers/admin_roles.py::roles_assign`
шлёт ПРИ НАЗНАЧЕНИИ роли) ЕЩЁ раз — за день до форума ГОРОДА, всем держателям capability
`checkin` этого города.

Архитектура — байт-в-байт тот же приём, что `services/checkin_broadcast.py` (вечерняя рассылка
QR): одна one-shot date-джоба НА ГОРОД (`checkin_volunteer_guide:{city}`), `replace_existing=
True`, джоба перечитывает состояние (аудиторию, текст, тумблер, дату форума) НА
СРАБАТЫВАНИИ — не то, что было верно на постановке. `reconcile()` вызывается и при старте бота
(`main.py`), и СРАЗУ после правки `forum_date`/тумблера/времени (`handlers/admin_settings.py`),
тот же трёхточечный шов, что у `services.checkin_broadcast.schedule_city_jobs`/
`reconcile_broadcasts`.

Аудитория — `handlers.admin_caps.capability_holders("checkin", city=city)`: тот же примитив,
что D-13 (notification fan-out), уже умеет и суперадминов (всегда), и привязанных/непривязанных
держателей права, и fallback «никто не привязан к этому городу -> все держатели» — свой второй
резолвер аудитории заводить незачем.

Идемпотентность — ПО ДНЮ ФОРУМА (`database.db.checkin_volunteer_guide_sends`,
`UNIQUE(telegram_id, day)`), НЕ по человеку раз и навсегда: волонтёр нескольких форумов города
в разные даты (или человек, который волонтёрил прошлый сезон и снова волонтёрит в этот)
получает напоминание перед КАЖДЫМ отдельным днём форума. Отправка ПРИ НАЗНАЧЕНИИ роли
(`roles_assign`) — независимая, более ранняя точка, эта джоба её не трогает и не заменяет (тот
же текст, другой повод: там — «тебе только что дали capability», здесь — «завтра форум»).

Тихие часы/«🔕» — та же логика, что у `services.checkin_broadcast` (D-35): НЕ действуют,
шпаргалка волонтёру — служебное сообщение персоналу, не рассылка делегату.

Пустой `checkin_volunteer_guide_text` (менеджер стёр текст) гасит джобу целиком (`schedule_
city_job` возвращает `reason="empty_text"`) — отправлять пустое сообщение персоналу нет
смысла, тот же приём, что у D-36 «каждая функция выключается тумблером», только тумблер здесь
неявный (пустой текст = выключено)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from database.db import checkin_volunteer_guide_mark_sent, checkin_volunteer_guide_sent_ids
from services import scheduler as _sched
from services.daily_digest import parse_time
from services.reject_rules import forum_date_for
from services.timeutil import msk_now
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_JOB_PREFIX = "checkin_volunteer_guide:"
_DEFAULT_TIME = "17:00"
_CAP = "checkin"


def job_id(city: str | None) -> str:
    return f"{_JOB_PREFIX}{city or 'all'}"


def guide_run_at(forum_date_ddmmyyyy: str | None, hhmm: str) -> datetime | None:
    """Накануне форума, в `hhmm` (D-33: дефолт 17:00 МСК). `None` — дата форума не задана или
    не парсится (защита от кривого значения, тот же приём, что `services.checkin_broadcast.
    _combine`/`evening_run_at`)."""
    if not forum_date_ddmmyyyy:
        return None
    try:
        day = datetime.strptime(forum_date_ddmmyyyy.strip(), "%d.%m.%Y")
    except (TypeError, ValueError, AttributeError):
        return None
    hours, minutes = parse_time(hhmm)
    return day.replace(hour=hours, minute=minutes) - timedelta(days=1)


async def broadcast_enabled_for(city: str | None) -> bool:
    from cities import get_setting_typed_for_city
    return await get_setting_typed_for_city(
        "checkin_volunteer_guide_broadcast_enabled", city,
    ) != "off"


async def _time_for(city: str | None) -> str:
    from cities import get_setting_typed_for_city
    t = await get_setting_typed_for_city("checkin_volunteer_guide_broadcast_time", city)
    return t or _DEFAULT_TIME


async def schedule_city_job(city: str | None) -> dict:
    """(Пере)ставить джобу ОДНОГО города — или снять её, если дата форума не задана, рассылка
    выключена (per_city тумблер), либо текст шпаргалки пуст. Вызывается и `reconcile()` (старт
    бота), и СРАЗУ после правки настройки (см. докстринг модуля)."""
    sched = _sched.get_scheduler()
    jid = job_id(city)

    date_str = await forum_date_for(city)
    if date_str is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": "no_date"}

    if not await broadcast_enabled_for(city):
        cancel_city_job(city)
        return {"scheduled": False, "reason": "disabled"}

    text = (await get_setting_typed("checkin_volunteer_guide_text") or "").strip()
    if not text:
        cancel_city_job(city)
        return {"scheduled": False, "reason": "empty_text"}

    hhmm = await _time_for(city)
    run_at = guide_run_at(date_str, hhmm)
    if run_at is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": "bad_date"}

    now = msk_now()
    if run_at <= now:
        # Правка настроек мимо джобы (менеджер выставил дату форума в прошлом, отредактировал
        # время после того, как оно уже прошло) — та же сделка, что у checkin_broadcast:
        # «сейчас + минута», а не молчаливая потеря рассылки.
        run_at = now + timedelta(minutes=1)

    sched.add_job(
        _run_job, "date", run_date=run_at, args=[city], id=jid, replace_existing=True,
    )
    return {"scheduled": True, "run_at": run_at}


def cancel_city_job(city: str | None) -> None:
    sched = _sched.get_scheduler()
    try:
        sched.remove_job(job_id(city))
    except Exception:
        pass  # уже сработала или не была поставлена — оба случая нормальные


async def _city_still_valid(city: str | None) -> bool:
    """Тот же барьер, что `services.checkin_broadcast._city_still_valid` — город/тумблер могли
    выключить МЕЖДУ постановкой джобы и её срабатыванием."""
    if city is not None:
        from cities import cities_module_on, enabled_cities
        if await cities_module_on():
            codes = {c["code"] for c in await enabled_cities()}
            if city not in codes:
                return False
    return await broadcast_enabled_for(city)


async def _run_job(city: str | None) -> dict:
    if not await _city_still_valid(city):
        logger.info(
            f"checkin_volunteer_broadcast: job for city={city!r} skipped — "
            "город/рассылка выключены к моменту срабатывания"
        )
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "disabled"}
    return await send_guide(city)


def _cancel_stale_city_jobs(enabled_codes: set[str]) -> None:
    sched = _sched.get_scheduler()
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
    `city=None`, если модуль городов выключен) — fail-soft НА ГОРОД, не блокирует старт бота
    целиком. Вызывается из `main.py` рядом с `services.checkin_broadcast.reconcile_broadcasts`."""
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
                logger.error(f"checkin_volunteer_broadcast.reconcile({code!r}) failed: {e}")
        if enabled_codes is not None:
            try:
                _cancel_stale_city_jobs(enabled_codes)
            except Exception as e:
                logger.error(f"checkin_volunteer_broadcast.reconcile: stale sweep failed: {e}")
    except Exception as e:
        logger.error(f"checkin_volunteer_broadcast.reconcile failed: {e}")
    return touched


async def send_guide(city: str | None) -> dict:
    """Date-джоба И (в будущем, если понадобится) ручной запуск — отправляет
    `checkin_volunteer_guide_text` всем держателям capability `checkin` города, кто ещё не
    получал его на ЭТОТ день форума (`checkin_volunteer_guide_sent_ids`)."""
    date_str = await forum_date_for(city)
    if date_str is None:
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "no_date"}

    text = (await get_setting_typed("checkin_volunteer_guide_text") or "").strip()
    if not text:
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "empty_text"}

    try:
        day = datetime.strptime(date_str.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except (TypeError, ValueError, AttributeError):
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "bad_date"}

    from handlers.admin_caps import capability_holders

    holders = await capability_holders(_CAP, city=city)
    already = await checkin_volunteer_guide_sent_ids(day)
    targets = [tid for tid in holders if tid not in already]

    sent = failed = 0
    for tid in targets:
        ok = await _sched._safe_send(lambda cid: _sched._bot.send_message(cid, text), tid)
        if ok:
            await checkin_volunteer_guide_mark_sent(
                tid, day, city, msk_now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    logger.info(
        f"checkin_volunteer_broadcast.send_guide({city!r}): sent {sent}, failed {failed} "
        f"of {len(targets)} (держателей {len(holders)}, уже было {len(already)})"
    )
    return {"sent": sent, "failed": failed, "total": len(targets)}
