"""Форум-ночь п.3 (D-03, идея №2): рассылка личного QR накануне форума + утренний повтор
неподтвердившим (`.planning/FORUM-CHECKIN.md` D-03, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`
раздел A2, идея №2).

per_city с самого начала (backlog, идея А1): 03.10 форум у СПб/Тюмени, Москва ещё набирает до
30.10 — общее время/тумблер отправили бы QR не тому городу не в тот день. Одна ПАРА one-shot
date-джоб НА ГОРОД (`checkin_qr_evening:{city}`/`checkin_qr_morning:{city}`) — тот же приём, что
`services.scheduler::schedule_wave_start`/`schedule_payment_reminder`: детерминированный id,
`replace_existing=True`, джоба перечитывает состояние (аудиторию, тексты, тумблер) НА
СРАБАТЫВАНИИ, а не то, что было верно на постановке.

Дата форума города — `services.reject_rules.forum_date_for` (уже существующий резолвер
per_city `forum_date`, Phase 31/D-30) — второй копии чтения этой настройки не заводим. Нет
даты форума у города -> джобы не ставятся вовсе (`schedule_city_jobs` снимает обе, если были).

Аудитория обеих рассылок — `database.db.list_approved_users(city_scope=...)`, отфильтрованная
`services.checkin.checkin_denial` НА КАЖДОЙ СТРОКЕ (единая точка правила допуска D-02 — не
вторая копия сезонного условия SQL-строкой). Идемпотентность вечерней рассылки И ручной кнопки
«📤 Разослать QR сейчас» (handlers/admin_checkin.py) — `database.db.checkin_qr_sent_ids` (кому
УЖЕ отправлен QR когда-либо) вычитается из пула ДО отправки, обе точки входа зовут ОДНУ и ту же
`send_broadcast`. Утренний повтор (`send_morning_repeat`, находка ревью 260924) шлёт ВСЕМ
допущенным города, кто ещё НЕ подтвердил «✅ Сохранил» (`eligible_recipients` минус
`checkin_qr_confirmed_ids`) — а не только тем, у кого уже есть строка `checkin_qr_sends`: делегат,
одобренный ПОСЛЕ вечерней рассылки, или чья вечерняя отправка сорвалась (сеть/бота заблокировал),
иначе терял бы QR навсегда. Для таких делегатов повтор САМ заводит первую строку
`checkin_qr_sends` (`checkin_qr_mark_sent`, идемпотентно) — это их первая отправка, счётчик
«QR получили N» обязан их учитывать.

Троттлинг отправки переиспользует `services.scheduler._safe_send` (тот же 429-safe single-retry
приём, что у рассылок/опросов/волн этого же файла) — не отдельный цикл ретраев.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database.db import (
    checkin_qr_confirm,
    checkin_qr_confirmed_ids,
    checkin_qr_mark_sent,
    checkin_qr_sent_ids,
    list_approved_users,
)
from services import scheduler as _sched
from services.checkin import build_checkin_qr, checkin_denial
from services.daily_digest import parse_time
from services.reject_rules import forum_date_for
from services.timeutil import msk_now
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# Кнопка-подтверждение на самой рассылке — фиксированный callback_data без embed'а telegram_id
# (aiogram отдаёт его в callback.from_user.id, второй параметр в data не нужен).
CONFIRM_CALLBACK = "checkinqr_confirm"

_EVENING_PREFIX = "checkin_qr_evening:"
_MORNING_PREFIX = "checkin_qr_morning:"

_DEFAULT_EVENING_TIME = "18:00"
_DEFAULT_MORNING_TIME = "08:00"


# ── Pure helpers (unit-test surface, без БД и без aiogram-вызовов) ───────────────────────────

def evening_job_id(city: str | None) -> str:
    return f"{_EVENING_PREFIX}{city or 'all'}"


def morning_job_id(city: str | None) -> str:
    return f"{_MORNING_PREFIX}{city or 'all'}"


def _combine(forum_date_ddmmyyyy: str, hhmm: str, *, days_offset: int) -> datetime | None:
    """«ДД.ММ.ГГГГ» (`services.reject_rules.forum_date_for`) + «ЧЧ:ММ» (`parse_time`, тот же
    парсер, что у `daily_digest_time`) -> datetime, сдвинутый на `days_offset` дней (-1 для
    вечерней рассылки накануне, 0 для утреннего повтора в день форума). `None` — дата не
    парсится (защита от кривого значения; в норме `forum_date_for` уже вернула валидную строку
    или `None` раньше)."""
    try:
        day = datetime.strptime((forum_date_ddmmyyyy or "").strip(), "%d.%m.%Y")
    except (TypeError, ValueError, AttributeError):
        return None
    hours, minutes = parse_time(hhmm)
    return day.replace(hour=hours, minute=minutes) + timedelta(days=days_offset)


def evening_run_at(forum_date_ddmmyyyy: str | None, hhmm: str) -> datetime | None:
    """Накануне форума, в `hhmm` (D-03: «по умолчанию накануне в 18:00 МСК»). Дата форума не
    задана -> `None` — «если даты форума у города нет — не ставится»."""
    if not forum_date_ddmmyyyy:
        return None
    return _combine(forum_date_ddmmyyyy, hhmm, days_offset=-1)


def morning_run_at(forum_date_ddmmyyyy: str | None, hhmm: str) -> datetime | None:
    """В САМ день форума, в `hhmm` (идея №2: «утром дня форума — повтор неподтвердившим»)."""
    if not forum_date_ddmmyyyy:
        return None
    return _combine(forum_date_ddmmyyyy, hhmm, days_offset=0)


# ── Настройки (мастер-тумблер + per_city) ────────────────────────────────────────────────────

async def broadcast_enabled_for(city: str | None) -> bool:
    """Мастер-тумблер `checkin_qr_enabled` ВЫКЛ -> QR вообще не выпускается (D-01), рассылки
    нет ни при каком per_city состоянии. Иначе — per_city `checkin_qr_broadcast_enabled`
    (дефолт "on", менеджер выключает конкретный город кнопкой)."""
    if await get_setting_typed("checkin_qr_enabled") != "on":
        return False
    from cities import get_setting_typed_for_city
    return await get_setting_typed_for_city("checkin_qr_broadcast_enabled", city) != "off"


async def _times_for(city: str | None) -> tuple[str, str]:
    from cities import get_setting_typed_for_city
    evening = await get_setting_typed_for_city("checkin_qr_broadcast_time", city)
    morning = await get_setting_typed_for_city("checkin_qr_morning_repeat_time", city)
    return evening or _DEFAULT_EVENING_TIME, morning or _DEFAULT_MORNING_TIME


# ── Планирование джоб (одна пара date-джоб на город) ─────────────────────────────────────────

async def schedule_city_jobs(city: str | None) -> dict:
    """(Пере)ставить вечернюю+утреннюю джобы ОДНОГО города — или снять обе, если дата форума не
    задана либо рассылка выключена (мастер/per_city). Вызывается и реконсиляцией на боте
    (`reconcile_broadcasts`, боевой рестарт), и СРАЗУ после правки `forum_date`/
    `checkin_qr_broadcast_enabled`/времени (handlers/admin_checkin.py, handlers/admin_settings.py)
    — «джоба переставляется при смене даты форума/настройки», без ожидания рестарта."""
    sched = _sched.get_scheduler()
    ev_id, morn_id = evening_job_id(city), morning_job_id(city)

    date_str = await forum_date_for(city)
    if date_str is None:
        cancel_city_jobs(city)
        return {"scheduled": False, "reason": "no_date"}

    if not await broadcast_enabled_for(city):
        cancel_city_jobs(city)
        return {"scheduled": False, "reason": "disabled"}

    ev_time, morn_time = await _times_for(city)
    now = msk_now()
    ev_at = evening_run_at(date_str, ev_time)
    morn_at = morning_run_at(date_str, morn_time)
    if ev_at is None or morn_at is None:
        # Дата форума не парсится, хотя formally не None (защита от кривого значения) —
        # то же самое, что «дата не задана», рассылку не ставим.
        cancel_city_jobs(city)
        return {"scheduled": False, "reason": "bad_date"}
    # Правка настроек мимо джобы (менеджер выставил дату форума в прошлом, отредактировал
    # время после того, как оно уже прошло) — та же сделка, что у wave_start/reg_digest: «сейчас
    # + минута», а не молчаливая потеря рассылки.
    if ev_at <= now:
        ev_at = now + timedelta(minutes=1)
    if morn_at <= now:
        morn_at = now + timedelta(minutes=1)

    sched.add_job(
        send_broadcast, "date", run_date=ev_at, args=[city],
        id=ev_id, replace_existing=True,
    )
    sched.add_job(
        send_morning_repeat, "date", run_date=morn_at, args=[city],
        id=morn_id, replace_existing=True,
    )
    return {"scheduled": True, "evening_at": ev_at, "morning_at": morn_at}


def cancel_city_jobs(city: str | None) -> None:
    sched = _sched.get_scheduler()
    for job_id in (evening_job_id(city), morning_job_id(city)):
        try:
            sched.remove_job(job_id)
        except Exception:
            pass  # уже сработала или не была поставлена — оба случая нормальные


async def reconcile_broadcasts() -> list[str | None]:
    """На боте: (пере)ставить джобы КАЖДОГО включённого города (или один общий проход
    `city=None`, если модуль городов выключен) — fail-soft НА ГОРОД, не блокирует старт бота
    целиком. Тот же приём, что `services.scheduler.reconcile_scheduled_broadcasts`/
    `reconcile_wave_jobs`: персистентный jobstore сам переживает обычный рестарт, этот проход
    нужен для случая «дата форума/настройка поменялась, пока бот не работал» и для первой
    постановки джобы города, которую ещё никто не трогал."""
    from cities import cities_module_on, enabled_cities

    touched: list[str | None] = []
    try:
        if await cities_module_on():
            codes = [c["code"] for c in await enabled_cities()]
        else:
            codes = [None]
        for code in codes:
            try:
                await schedule_city_jobs(code)
                touched.append(code)
            except Exception as e:
                logger.error(f"checkin_broadcast.reconcile_broadcasts({code!r}) failed: {e}")
    except Exception as e:
        logger.error(f"checkin_broadcast.reconcile_broadcasts failed: {e}")
    return touched


# ── Аудитория ─────────────────────────────────────────────────────────────────────────────

async def eligible_recipients(city: str | None) -> list[dict]:
    """Одобренные текущего сезона города — через `services.checkin.checkin_denial` НА КАЖДОЙ
    строке (единая точка правила допуска D-02), не отдельная копия сезонного условия. Общий
    пул для вечерней/ручной рассылки И для превью счётчика (`pending_broadcast_count`)."""
    import cities as _cities

    candidates = await list_approved_users(city_scope=_cities.city_scope(city))
    eligible = []
    for user in candidates:
        if await checkin_denial(user) is None:
            eligible.append(user)
    return eligible


async def pending_broadcast_count(city: str | None) -> int:
    """Сколько делегатов города РЕАЛЬНО получат QR при следующей отправке (вечерней джобе или
    ручной кнопке «📤 Разослать QR сейчас») — превью для подтверждения «Уйдёт N делегатам
    города X» (handlers/admin_checkin.py)."""
    import cities as _cities

    eligible = await eligible_recipients(city)
    already = await checkin_qr_sent_ids(city_scope=_cities.city_scope(city))
    return sum(1 for u in eligible if u["telegram_id"] not in already)


# ── Отправка ──────────────────────────────────────────────────────────────────────────────

def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранил, открывается", callback_data=CONFIRM_CALLBACK),
    ]])


async def _translated_caption(telegram_id: int, text: str) -> str:
    """Тот же перевод, что у остальных ответов делегату (`handlers.reg_i18n.tr_text`,
    `show_my_checkin_qr`) — ленивый импорт: этот модуль зовётся из джоб-таргетов
    `services/scheduler.py`, которые уже лениво тянут `handlers.*` внутри функций (Pitfall
    циклического импорта на уровне модуля, см. докстринг `services/reg_digest.py`)."""
    from handlers.reg_i18n import tr_text
    from services import i18n as i18n_service

    lang, tr_map = await i18n_service.context(telegram_id)
    return tr_text(text, lang, tr_map)


async def _send_one(telegram_id: int, png: bytes, caption: str) -> bool:
    async def _factory(cid):
        return await _sched._bot.send_photo(
            cid, BufferedInputFile(png, filename="checkin_qr.png"),
            caption=caption, reply_markup=_confirm_kb(),
        )
    return await _sched._safe_send(_factory, telegram_id)


async def send_broadcast(city: str | None) -> dict:
    """Date-джоба вечерней рассылки И ручная кнопка «📤 Разослать QR сейчас»
    (handlers/admin_checkin.py) — ОДНА и та же функция, идемпотентная по построению:
    `checkin_qr_sent_ids` вычитается из пула ДО отправки, повторный вызов (рестарт бота,
    двойной тап кнопки) не находит уже отправленных заново."""
    import cities as _cities

    scope = _cities.city_scope(city)
    eligible = await eligible_recipients(city)
    already = await checkin_qr_sent_ids(city_scope=scope)
    targets = [u for u in eligible if u["telegram_id"] not in already]

    from cities import get_setting_typed_for_city
    base_text = await get_setting_typed_for_city("checkin_qr_broadcast_text", city)

    sent = failed = 0
    for user in targets:
        tid = user["telegram_id"]
        try:
            png, _default_caption = await build_checkin_qr(user)
            caption = await _translated_caption(tid, base_text)
        except Exception as e:
            logger.error(f"checkin_broadcast.send_broadcast: build for {tid} failed: {e}")
            failed += 1
            continue
        ok = await _send_one(tid, png, caption)
        if ok:
            await checkin_qr_mark_sent(
                tid, user.get("event_city"), msk_now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    logger.info(
        f"checkin_broadcast.send_broadcast({city!r}): sent {sent}, failed {failed} "
        f"of {len(targets)} (пул {len(eligible)}, уже было {len(already)})"
    )
    return {"sent": sent, "failed": failed, "total": len(targets)}


async def send_morning_repeat(city: str | None) -> dict:
    """Date-джоба утреннего повтора (идея №2) — ВСЕМ допущенным делегатам города, кто ещё НЕ
    подтвердил «✅ Сохранил» (находка ревью 260924): `eligible_recipients` — тот же живой пул,
    что у вечерней рассылки (перечитан на срабатывании, не снимок вечера) — минус
    `checkin_qr_confirmed_ids`. Это НАМЕРЕННО шире, чем «кому отправлен вечером» — делегат,
    одобренный ПОСЛЕ вечерней рассылки, или чья вечерняя отправка сорвалась (никогда не
    получил строку `checkin_qr_sends`), получает QR СЕЙЧАС, а не теряет его навсегда.
    `checkin_qr_mark_sent` внутри цикла — идемпотентная (`INSERT OR IGNORE`): для уже
    отправленных вечером не создаёт вторую строку и не двигает `sent_at`, а для НИКОГДА не
    отправленных заводит первую (счётчик «QR получили N» обязан их учитывать)."""
    import cities as _cities

    scope = _cities.city_scope(city)
    eligible = await eligible_recipients(city)
    confirmed = await checkin_qr_confirmed_ids(city_scope=scope)
    targets = [u for u in eligible if u["telegram_id"] not in confirmed]

    from cities import get_setting_typed_for_city
    base_text = await get_setting_typed_for_city("checkin_qr_broadcast_text", city)

    sent = failed = 0
    for user in targets:
        tid = user["telegram_id"]
        try:
            png, _default_caption = await build_checkin_qr(user)
            caption = await _translated_caption(tid, base_text)
        except Exception as e:
            logger.error(f"checkin_broadcast.send_morning_repeat: build for {tid} failed: {e}")
            failed += 1
            continue
        ok = await _send_one(tid, png, caption)
        if ok:
            await checkin_qr_mark_sent(
                tid, user.get("event_city"), msk_now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    logger.info(
        f"checkin_broadcast.send_morning_repeat({city!r}): sent {sent}, failed {failed} "
        f"of {len(targets)} (пул {len(eligible)}, уже подтвердили {len(confirmed)})"
    )
    return {"sent": sent, "failed": failed, "total": len(targets)}


# ── Подтверждение «✅ Сохранил, открывается» ─────────────────────────────────────────────────

async def confirm_receipt(telegram_id: int) -> bool:
    """`True` — это ПЕРВОЕ подтверждение этого делегата (счётчик «подтвердили» вырос);
    `False` — уже было подтверждено раньше (двойной тап) или строки нет вовсе (делегат никогда
    не получал QR этой рассылкой). Оба случая отвечают делегату одинаково дружелюбно —
    хендлер (handlers/user_actions.py) не обязан различать их в тексте."""
    return await checkin_qr_confirm(telegram_id, msk_now().strftime("%Y-%m-%d %H:%M:%S"))
