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
from datetime import datetime, time, timedelta

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
# Насколько утренний повтор может опоздать и всё ещё уйти (рестарт бота ровно в его время).
_MORNING_CATCHUP = timedelta(minutes=2)
# Потолок догона накануне форума: позже 22:00 МСК «через минуту» не шлём ни QR, ни шпаргалку
# волонтёру — ночное служебное сообщение (тихие часы на него не действуют) будит людей. QR
# подберёт утренний повтор неподтвердившим, шпаргалка уйдёт утром дня форума в то же время.
# Не настройка: это граница приличия, а не параметр мероприятия.
EVENING_CATCHUP_CUTOFF = time(22, 0)


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
    # Догон «сейчас + минута» — только пока форум не начался. Раньше прошедшее время
    # переносилось безусловно, и после даты форума КАЖДЫЙ рестарт бота (reconcile на старте)
    # слал QR неподтвердившим — мимо тихих часов, хоть в три ночи.
    forum_day, today = morn_at.date(), now.date()
    if forum_day < today:
        cancel_city_jobs(city)
        return {"scheduled": False, "reason": "past"}

    # Вечер накануне: прошёл, а форум завтра или позже (менеджер поздно включил) — догоняем,
    # но не позже EVENING_CATCHUP_CUTOFF; форум уже сегодня или на часах за 22:00 — вечернюю
    # не ставим, её работу сделает утренний повтор.
    # Отработавшую вечернюю рассылку не перевзводим: сверка каждые 10 минут иначе гоняла её до
    # 22:00 заново, и заблокировавшим бота отправка пробовалась ~24 раза.
    if ev_at <= now:
        late = now.time() >= EVENING_CATCHUP_CUTOFF
        done = _evening_done.get(city) == date_str
        ev_at = now + timedelta(minutes=1) if forum_day > today and not late and not done else None
    # Утренний повтор: только в день форума и только пока его время впереди. Догон — лишь для
    # ещё не сработавшей джобы, опоздавшей не больше чем на `_MORNING_CATCHUP` (рестарт в
    # 08:01); без проверки «джоба ещё в хранилище» реконсиляция сразу после срабатывания
    # отправила бы повтор второй раз.
    if morn_at <= now:
        pending = sched.get_job(morn_id) is not None
        if pending and now - morn_at <= _MORNING_CATCHUP:
            morn_at = now + timedelta(minutes=1)
        else:
            morn_at = None

    for jid, target, run_at in (
        (ev_id, _run_evening_job, ev_at), (morn_id, _run_morning_job, morn_at),
    ):
        if run_at is None:
            try:
                sched.remove_job(jid)
            except Exception:
                pass
        else:
            sched.add_job(
                target, "date", run_date=run_at, args=[city], id=jid, replace_existing=True,
            )
    return {
        "scheduled": ev_at is not None or morn_at is not None,
        "evening_at": ev_at, "morning_at": morn_at,
    }


# Город -> дата форума, на которую вечерняя рассылка уже отработала в этом процессе. В памяти:
# после рестарта догон сработает ещё один раз — идемпотентно (`checkin_qr_sent_ids`), а
# заблокировавшие бота получат одну лишнюю попытку, а не по одной каждые 10 минут.
_evening_done: dict[str | None, str] = {}


def cancel_city_jobs(city: str | None) -> None:
    sched = _sched.get_scheduler()
    for job_id in (evening_job_id(city), morning_job_id(city)):
        try:
            sched.remove_job(job_id)
        except Exception:
            pass  # уже сработала или не была поставлена — оба случая нормальные


async def _city_still_valid(city: str | None) -> bool:
    """Находка ревью 260924: город/тумблер могли выключить МЕЖДУ постановкой джобы и её
    срабатыванием (`reconcile_broadcasts` снимает джобы уже выключенных городов только НА
    МОМЕНТ своего обхода — старт бота или правка `forum_date`, — а не непрерывно). Тот же
    приём, что «джоба перед отправкой перечитывает payment_status» (CLAUDE.md, платёжные
    напоминания): персистентная джоба (`_run_evening_job`/`_run_morning_job`) перепроверяет
    состояние САМА в момент срабатывания, а не доверяет тому, что было верно на постановке.
    `send_broadcast`/`send_morning_repeat` НАПРЯМУЮ (ручная кнопка «📤 Разослать QR сейчас»,
    юнит-тесты) этот барьер не проходят — менеджер, явно нажавший кнопку в своём городе, не
    должен упереться в гонку, которой физически нет."""
    if city is not None:
        from cities import cities_module_on, enabled_cities
        if await cities_module_on():
            codes = {c["code"] for c in await enabled_cities()}
            if city not in codes:
                return False
    return await broadcast_enabled_for(city)


async def is_forum_day_offset(city: str | None, days_before: int) -> bool:
    """Сегодня — день форума города минус `days_before` (1 — накануне, 0 — сам день)?
    Дату перечитываем на срабатывании: Mini App — отдельный процесс без планировщика, его
    правка `forum_date` не переставляет джобы, и старая джоба сработала бы не в тот день."""
    date_str = await forum_date_for(city)
    try:
        day = datetime.strptime((date_str or "").strip(), "%d.%m.%Y").date()
    except (TypeError, ValueError, AttributeError):
        return False
    return msk_now().date() == day - timedelta(days=days_before)


async def _wrong_day(city: str | None, days_before: int, what: str) -> bool:
    """Не тот день — пропуск без отметки «отправлено» и перестановка по свежим настройкам."""
    if await is_forum_day_offset(city, days_before):
        return False
    logger.info(f"checkin_broadcast: {what} job for city={city!r} skipped — не тот день форума")
    try:
        await schedule_city_jobs(city)
    except Exception as e:
        logger.error(f"checkin_broadcast: reschedule after wrong day ({city!r}) failed: {e}")
    return True


async def _run_evening_job(city: str | None) -> dict:
    """Персистентный jobstore-таргет вечерней рассылки (`schedule_city_jobs` регистрирует
    ИМЕННО эту функцию, не `send_broadcast` напрямую) — барьер `_city_still_valid` ПЕРЕД
    вызовом, см. её докстринг, и проверка, что сегодня канун форума."""
    if not await _city_still_valid(city):
        logger.info(
            f"checkin_broadcast: evening job for city={city!r} skipped — "
            "город/рассылка выключены к моменту срабатывания"
        )
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "disabled"}
    if await _wrong_day(city, 1, "evening"):
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "wrong_day"}
    result = await send_broadcast(city)
    if not result.get("already_running"):
        date_str = await forum_date_for(city)
        if date_str:
            _evening_done[city] = date_str
    return result


async def _run_morning_job(city: str | None) -> dict:
    """То же самое для утреннего повтора — см. `_run_evening_job`/`_city_still_valid`;
    уходит только в сам день форума."""
    if not await _city_still_valid(city):
        logger.info(
            f"checkin_broadcast: morning job for city={city!r} skipped — "
            "город/рассылка выключены к моменту срабатывания"
        )
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "disabled"}
    if await _wrong_day(city, 0, "morning"):
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "wrong_day"}
    return await send_morning_repeat(city)


def _cancel_stale_city_jobs(enabled_codes: set[str]) -> None:
    """Находка ревью 260924: `reconcile_broadcasts` обходит только `enabled_cities()` — у
    города, выключенного ПОСЛЕ того, как его джобы были поставлены, обе джобы
    (`checkin_qr_evening:{code}`/`checkin_qr_morning:{code}`) остаются висеть в персистентном
    jobstore и сработают как ни в чём не бывало (`_run_evening_job`/`_run_morning_job` их,
    конечно, перехватят через `_city_still_valid` — но джоба-призрак в списке при живом боте не
    место). Перебор `scheduler.get_jobs()` по префиксу id, а не по списку кодов — кода
    выключенного города у нас уже нет, только сам факт, что он не входит в `enabled_codes`.
    `checkin_qr_evening:all`/`checkin_qr_morning:all` (сентинел «модуль городов выключен») не
    трогаем — эта пара живёт вне понятия «включённый город»."""
    sched = _sched.get_scheduler()
    for job in sched.get_jobs():
        for prefix in (_EVENING_PREFIX, _MORNING_PREFIX):
            if not job.id.startswith(prefix):
                continue
            code = job.id[len(prefix):]
            if code != "all" and code not in enabled_codes:
                try:
                    sched.remove_job(job.id)
                except Exception:
                    pass
            break


async def reconcile_broadcasts() -> list[str | None]:
    """На боте: (пере)ставить джобы КАЖДОГО включённого города (или один общий проход
    `city=None`, если модуль городов выключен) — fail-soft НА ГОРОД, не блокирует старт бота
    целиком. Тот же приём, что `services.scheduler.reconcile_scheduled_broadcasts`/
    `reconcile_wave_jobs`: персистентный jobstore сам переживает обычный рестарт, этот проход
    нужен для случая «дата форума/настройка поменялась, пока бот не работал» и для первой
    постановки джобы города, которую ещё никто не трогал. Плюс (находка ревью 260924)
    `_cancel_stale_city_jobs` — снимает джобы городов, выключенных с прошлого обхода."""
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
                await schedule_city_jobs(code)
                touched.append(code)
            except Exception as e:
                logger.error(f"checkin_broadcast.reconcile_broadcasts({code!r}) failed: {e}")
        if enabled_codes is not None:
            try:
                _cancel_stale_city_jobs(enabled_codes)
            except Exception as e:
                logger.error(f"checkin_broadcast.reconcile_broadcasts: stale sweep failed: {e}")
    except Exception as e:
        logger.error(f"checkin_broadcast.reconcile_broadcasts failed: {e}")
    return touched


async def reconcile_forum_jobs() -> None:
    """Сверка обеих форумных рассылок — QR делегатам и шпаргалки волонтёру (обе зависят от
    `forum_date` и мастера `checkin_qr_enabled`). Идемпотентна; fail-soft — сбой планировщика
    не должен ронять ни переключение тумблера, ни периодическую джобу."""
    try:
        await reconcile_broadcasts()
    except Exception as e:
        logger.error(f"reconcile_forum_jobs: checkin_broadcast failed: {e}")
    try:
        from services.checkin_volunteer_broadcast import reconcile
        await reconcile()
    except Exception as e:
        logger.error(f"reconcile_forum_jobs: checkin_volunteer_broadcast failed: {e}")


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
#
# D-35 (решение владельца 24.09): QR накануне — СЛУЖЕБНОЕ сообщение, не рассылка. Тихие часы
# на него НЕ действуют (до этой правки рассылка города откладывалась целиком до конца окна
# тихих часов, находка ревью 260924 п.3 — владелец 24.09 явно отменил это поведение для QR:
# делегату он нужен независимо от часа, площадка/сеть на форуме не ждут утра) и «🔕 Не
# присылать сегодня» тоже НЕ фильтрует получателей — `send_broadcast`/`send_morning_repeat`
# ниже НИКОГДА не проверяют ни `services.quiet_hours`, ни список заглушивших «🔕» из `database.db`,
# в отличие от обычных рассылок (`services/scheduler.py::send_scheduled_broadcast`,
# `handlers/admin_broadcasts.py::bc_go`) — это НЕ упущение, а осознанное отличие служебного
# сообщения от рассылки.

def _confirm_kb(lang: str = "ru", tr_map: dict | None = None) -> InlineKeyboardMarkup:
    """Кнопка под QR — на языке получателя (EN-делегат видел «✅ Сохранил, открывается»).
    Ленивый импорт: этот модуль зовётся из джоб-таргетов `services/scheduler.py`, которые уже
    лениво тянут `handlers.*` внутри функций (Pitfall циклического импорта на уровне модуля,
    см. докстринг `services/reg_digest.py`)."""
    from handlers.reg_i18n import tr_text

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=tr_text("✅ Сохранил, открывается", lang, tr_map or {}),
            callback_data=CONFIRM_CALLBACK,
        ),
    ]])


async def _render_for(
    telegram_id: int, text: str, maps: dict[str, dict],
) -> tuple[str, InlineKeyboardMarkup]:
    """Подпись и кнопка на языке получателя — тот же перевод, что у остальных ответов
    делегату (`handlers.reg_i18n.tr_text`, `show_my_checkin_qr`). `maps` — карты переводов
    на всю рассылку (`services.i18n.context_cached`), не выборка на каждого."""
    from handlers.reg_i18n import tr_text
    from services import i18n as i18n_service

    lang, tr_map = await i18n_service.context_cached(telegram_id, maps)
    return tr_text(text, lang, tr_map), _confirm_kb(lang, tr_map)


async def _broadcast_text(city: str | None, *, morning: bool) -> str:
    """Текст под QR. Утренний повтор — всегда `checkin_qr_morning_text` (без «Завтра форум!»).
    Вечерняя/ручная рассылка в сам день форума (догон, «📤 Разослать QR сейчас» утром) — тоже
    утренний текст: «завтра» в день форума путает делегатов."""
    from cities import get_setting_typed_for_city
    if not morning:
        morning = await is_forum_day_offset(city, 0)
    key = "checkin_qr_morning_text" if morning else "checkin_qr_broadcast_text"
    return await get_setting_typed_for_city(key, city)


async def _send_one(telegram_id: int, png: bytes, caption: str, kb: InlineKeyboardMarkup) -> bool:
    async def _factory(cid):
        return await _sched._bot.send_photo(
            cid, BufferedInputFile(png, filename="checkin_qr.png"),
            caption=caption, reply_markup=kb,
        )
    return await _sched._safe_send(_factory, telegram_id)


# Находка ревью 260924 (п.4): «📤 Разослать QR сейчас» отвечает на callback СРАЗУ (T-12-03,
# рассылка может занять минуты), поэтому кнопки подтверждения физически успевают остаться
# нажимаемыми на экране менеджера, пока идёт долгая отправка — второй тап (двойной клик,
# задумчивость) запускал бы параллельный `send_broadcast` того же города с дублями рассылки.
# Барьер здесь — второй, за хендлером (handlers/admin_checkin.py убирает клавиатуру ДО вызова,
# см. `checkinqr_send_go`): `asyncio.Lock` НА ГОРОД, не глобальный (разные города рассылаются
# независимо друг от друга). Простой `dict` (не `defaultdict`) — создаётся лениво, единственный
# читатель/писатель — этот же однопоточный event loop, гонки на СОЗДАНИЕ лока нет (между
# `.get`/`setdefault` нет await).
_city_locks: dict[str | None, asyncio.Lock] = {}


def _get_city_lock(city: str | None) -> asyncio.Lock:
    lock = _city_locks.get(city)
    if lock is None:
        lock = asyncio.Lock()
        _city_locks[city] = lock
    return lock


async def send_broadcast(city: str | None) -> dict:
    """Date-джоба вечерней рассылки И ручная кнопка «📤 Разослать QR сейчас»
    (handlers/admin_checkin.py) — ОДНА и та же функция, идемпотентная по построению:
    `checkin_qr_sent_ids` вычитается из пула ДО отправки, повторный вызов (рестарт бота,
    двойной тап кнопки) не находит уже отправленных заново.

    Тихие часы делегатов НЕ действуют (D-35, 24.09) — QR служебное сообщение, отправляется
    независимо от часа.

    Двойной тап «Разослать сейчас» (находка ревью 260924, п.4) — `lock.locked()` уже True, пока
    рассылка этого же города в процессе -> второй вызов НЕМЕДЛЕННО возвращает
    `already_running`, не встаёт в очередь на лок и не трогает БД вовсе."""
    lock = _get_city_lock(city)
    if lock.locked():
        logger.info(f"checkin_broadcast.send_broadcast({city!r}): уже идёт — повторный вызов отклонён")
        return {"sent": 0, "failed": 0, "total": 0, "already_running": True}

    async with lock:
        import cities as _cities

        scope = _cities.city_scope(city)
        eligible = await eligible_recipients(city)
        already = await checkin_qr_sent_ids(city_scope=scope)
        targets = [u for u in eligible if u["telegram_id"] not in already]

        base_text = await _broadcast_text(city, morning=False)

        sent = failed = 0
        tr_maps: dict[str, dict] = {}
        for user in targets:
            tid = user["telegram_id"]
            try:
                png, _default_caption = await build_checkin_qr(user)
                caption, kb = await _render_for(tid, base_text, tr_maps)
            except Exception as e:
                logger.error(f"checkin_broadcast.send_broadcast: build for {tid} failed: {e}")
                failed += 1
                continue
            ok = await _send_one(tid, png, caption, kb)
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
    отправленных заводит первую (счётчик «QR получили N» обязан их учитывать).

    Тихие часы делегатов НЕ действуют (D-35, 24.09) — служебное сообщение, см. докстринг
    `send_broadcast`."""
    import cities as _cities

    scope = _cities.city_scope(city)
    eligible = await eligible_recipients(city)
    confirmed = await checkin_qr_confirmed_ids(city_scope=scope)
    targets = [u for u in eligible if u["telegram_id"] not in confirmed]

    base_text = await _broadcast_text(city, morning=True)

    sent = failed = 0
    tr_maps: dict[str, dict] = {}
    for user in targets:
        tid = user["telegram_id"]
        try:
            png, _default_caption = await build_checkin_qr(user)
            caption, kb = await _render_for(tid, base_text, tr_maps)
        except Exception as e:
            logger.error(f"checkin_broadcast.send_morning_repeat: build for {tid} failed: {e}")
            failed += 1
            continue
        ok = await _send_one(tid, png, caption, kb)
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
