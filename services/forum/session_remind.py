"""Напоминания делегату о сессиях программы.

1. «⏰ За N минут до сессии» — личное сообщение тому, кто на неё записан (`session_enrollments`),
   с залом и компетенциями. Своя джоба на каждую сессию (`session_remind:{id}`).
2. «☀️ Программа на день» — утром в день программы список сессий делегата (его записи плюс общие
   сессии без трека — тот же `session_enroll.my_schedule`, что показывает «Моё расписание»).
   Своя джоба на пару город+день (`session_morning:{город}:{день}`).

Время сессий и утренней отправки — МЕСТНОЕ время города, планировщик живёт по Москве, поэтому момент
джобы сдвигается на `city_tz_offset` (тот же приём, что `services/session_feedback.py`).

Идемпотентность — журнал `session_remind_sends` (`database/session_remind_db.py`): перезапуск,
перепланирование и повторный тик не дублируют сообщения. Джоба перед отправкой перечитывает тумблер,
сессию и записи: отписался, сессию удалили или перенесли — сообщение не уйдёт.

Дедлайны: напоминание не уходит позже начала сессии, утренняя программа — позже начала первой
сессии дня (бот был выключен — догнали, но не задним числом).

aiogram-free на уровне импорта: планировщик, бот и i18n подтягиваются лениво внутри функций,
которые вызывает только бот."""
from __future__ import annotations

import asyncio
import html as html_module
import logging
from datetime import datetime, timedelta

from database import session_remind_db as rdb
from domain.cities import get_setting_typed_for_city
from services.infra.timeutil import city_offset_hours

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 10
MINUTES_CHOICES = (5, 10, 15, 30)
DEFAULT_MORNING_TIME = "08:30"
_MAX_MINUTES = 360
_THROTTLE_SECONDS = 0.05
_REMIND_PREFIX = "session_remind:"
_MORNING_PREFIX = "session_morning:"


def remind_job_id(session_id: int) -> str:
    return f"{_REMIND_PREFIX}{int(session_id)}"


def morning_job_id(city: str, day: str) -> str:
    return f"{_MORNING_PREFIX}{city}:{day}"


# ── Чистые помощники ─────────────────────────────────────────────────────────────────────────

def parse_hhmm(raw) -> tuple[int, int] | None:
    """«08:30» / «8:30» -> (8, 30); всё остальное -> None."""
    try:
        hh, mm = str(raw).strip().split(":")
        hours, minutes = int(hh), int(mm)
    except (AttributeError, TypeError, ValueError):
        return None
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return hours, minutes
    return None


def _local_dt(day: str, hhmm: str) -> datetime | None:
    try:
        return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def session_start_msk(session: dict, offset_hours: int) -> datetime | None:
    """Начало сессии по Москве (время сессии — местное время города)."""
    start = _local_dt(session.get("day"), session.get("start_time"))
    return None if start is None else start - timedelta(hours=offset_hours)


def remind_run_at(session: dict, minutes: int, offset_hours: int) -> datetime | None:
    """Момент напоминания по Москве: начало сессии минус N минут, минус смещение города."""
    start = session_start_msk(session, offset_hours)
    return None if start is None else start - timedelta(minutes=minutes)


def morning_run_at(day: str, hhmm: str, offset_hours: int) -> datetime | None:
    parsed = parse_hhmm(hhmm) or parse_hhmm(DEFAULT_MORNING_TIME)
    base = _local_dt(day, f"{parsed[0]:02d}:{parsed[1]:02d}")
    return None if base is None else base - timedelta(hours=offset_hours)


def clamp_minutes(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MINUTES
    return value if 1 <= value <= _MAX_MINUTES else DEFAULT_MINUTES


# ── Настройки ────────────────────────────────────────────────────────────────────────────────

async def remind_enabled(city: str | None) -> bool:
    return await get_setting_typed_for_city("session_remind_enabled", city) == "on"


async def morning_enabled(city: str | None) -> bool:
    return await get_setting_typed_for_city("session_morning_enabled", city) == "on"


async def remind_minutes(city: str | None) -> int:
    return clamp_minutes(await get_setting_typed_for_city("session_remind_minutes", city))


async def morning_time(city: str | None) -> str:
    raw = await get_setting_typed_for_city("session_morning_time", city)
    parsed = parse_hhmm(raw) or parse_hhmm(DEFAULT_MORNING_TIME)
    return f"{parsed[0]:02d}:{parsed[1]:02d}"


# ── Тексты ───────────────────────────────────────────────────────────────────────────────────

def _esc(value) -> str:
    return html_module.escape(str(value or ""), quote=False)


def _translate(text: str, lang: str, tr_map: dict | None) -> str:
    if lang == "ru" or not tr_map:
        return text
    # То же, что `handlers.i18n.reg_i18n.tr_text` без прогресс-префикса: ведущий эмодзи не
    # переводится, ядро идёт через общий `i18n.tr` (сервисы handlers не импортируют).
    from services.i18n import i18n as i18n_service
    from services.i18n.i18n_glossary import split_leading_symbols

    prefix, core = split_leading_symbols(text)
    translated = i18n_service.tr(core, lang, tr_map)
    return f"{prefix}{translated}" if prefix else translated


async def _hall_name(session: dict) -> str:
    if session.get("hall_name") is not None:
        return session.get("hall_name") or ""
    hall_id = session.get("hall_id")
    if hall_id is None:
        return ""
    from database.db import get_program_hall

    hall = await get_program_hall(hall_id)
    return (hall or {}).get("name") or ""


async def build_remind_text(
    session: dict, minutes: int, *, lang: str = "ru", tr_map: dict | None = None,
    competencies: list[str] | None = None,
) -> str:
    """Текст напоминания. Нет зала или компетенций — строка выпадает целиком, без пустых кавычек."""
    from database import session_enroll_db as edb
    from services.forum.session_enroll import compact_template

    city = session.get("city")
    template = await get_setting_typed_for_city("session_remind_text", city)
    if competencies is None:
        competencies = (await edb.competency_names_for_sessions([session["id"]])).get(session["id"], [])
    values = {
        "minutes": str(minutes),
        "title": _esc((session.get("title") or "").strip() or "сессия"),
        "start": _esc(session.get("start_time")),
        "hall": _esc(await _hall_name(session)),
        "competencies": _esc(", ".join(competencies)),
    }
    return compact_template(_translate(template, lang, tr_map), values)


def _program_line(item: dict, hall: str, competencies: list[str], common_label: str) -> str:
    s = item["session"]
    end = s.get("end_time")
    when = f"{s['start_time']}–{end}" if end else str(s["start_time"])
    parts = [f"🕒 {when}", f"«{_esc((s.get('title') or '').strip() or 'сессия')}»"]
    if hall:
        parts.append(f"зал {_esc(hall)}")
    if competencies:
        parts.append(f"🎯 {_esc(', '.join(competencies))}")
    line = " · ".join(parts)
    if item.get("kind") == "common" and common_label:
        line += f" ({_esc(common_label)})"
    return line


async def build_morning_text(
    city: str, items: list[dict], *, lang: str = "ru", tr_map: dict | None = None,
) -> str:
    """Шапка + по строке на сессию (по времени). Общие сессии помечаются как в «Моём расписании»."""
    from database import session_enroll_db as edb

    head = await get_setting_typed_for_city("session_morning_head", city)
    common_label = await get_setting_typed_for_city("session_enroll_common_label", city)
    comps = await edb.competency_names_for_sessions([i["session"]["id"] for i in items])
    lines = [_translate(head, lang, tr_map)]
    cl = _translate(common_label or "", lang, tr_map)
    for item in items:
        s = item["session"]
        hall = await _hall_name(s)
        lines.append(_program_line(item, hall, comps.get(s["id"], []), cl))
    return "\n".join(lines)


# ── Получатели ───────────────────────────────────────────────────────────────────────────────

async def _city_delegates(session_users: list[dict], city: str) -> list[dict]:
    """Только одобренные текущего сезона города (`checkin_denial`) — импортированные делегаты
    прошлого сезона напоминаний не получают."""
    from domain.cities import normalize_city
    from services.forum.checkin import checkin_denial

    out = []
    for user in session_users:
        if normalize_city(user.get("event_city")) != city:
            continue
        if await checkin_denial(user) is not None:
            continue
        out.append(user)
    return out


async def remind_recipients(session: dict) -> list[dict]:
    return await _city_delegates(await rdb.list_enrolled_approved(session["id"]), session["city"])


async def morning_items_for_user(telegram_id: int, city: str, day: str) -> list[dict]:
    from services.forum.session_enroll import my_schedule

    for block in await my_schedule(telegram_id, city):
        if block["day"] == day:
            return block["items"]
    return []


# ── Отправка ─────────────────────────────────────────────────────────────────────────────────

async def _send(telegram_id: int, text: str, kind: str, ref: str, now: datetime) -> bool:
    """Резерв в журнале -> отправка. Заблокировавший бота остаётся в журнале (повторять нечего),
    временный сбой резерв снимает."""
    from services import scheduler as sched

    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    if not await rdb.reserve(telegram_id, kind, ref, stamp):
        return False
    bot = sched.get_bot()
    permanent: list[int] = []

    async def _remember(cid):
        permanent.append(cid)

    ok = await sched._safe_send(
        lambda cid: bot.send_message(cid, text), telegram_id, on_permanent_failure=_remember,
    )
    if not ok and not permanent:
        await rdb.release(telegram_id, kind, ref)
    return ok


async def _lang_for(telegram_id: int, tr_maps: dict) -> tuple[str, dict]:
    try:
        from services.i18n import i18n as i18n_service

        return await i18n_service.context_cached(telegram_id, tr_maps)
    except Exception:
        return "ru", {}


async def deliver_session_reminders(session_id: int) -> dict:
    """Тело джобы напоминания. Возвращает {"sent", "skipped"} (для тестов и логов)."""
    result = {"sent": 0, "skipped": 0}
    try:
        from database.db import get_program_session
        from services.scheduler import _now_moscow_naive

        session = await get_program_session(session_id)
        if session is None:
            return result
        city = session["city"]
        if not await remind_enabled(city):
            return result
        offset = await city_offset_hours(city)
        start = session_start_msk(session, offset)
        now = _now_moscow_naive()
        if start is None or now >= start:
            return result  # поздно: сессия уже началась
        minutes = await remind_minutes(city)
        run_at = remind_run_at(session, minutes, offset)
        if run_at is not None and now < run_at - timedelta(minutes=1):
            return result  # старая джоба (время/минуты поменяли): актуальную поставит перепланирование
        from database import session_enroll_db as edb

        competencies = (await edb.competency_names_for_sessions([session_id])).get(session_id, [])
        tr_maps: dict = {}
        for user in await remind_recipients(session):
            tid = int(user["telegram_id"])
            if await rdb.was_sent(tid, rdb.KIND_REMIND, str(session_id)):
                result["skipped"] += 1
                continue
            lang, tr_map = await _lang_for(tid, tr_maps)
            text = await build_remind_text(session, minutes, lang=lang, tr_map=tr_map, competencies=competencies)
            if await _send(tid, text, rdb.KIND_REMIND, str(session_id), now):
                result["sent"] += 1
            else:
                result["skipped"] += 1
            await asyncio.sleep(_THROTTLE_SECONDS)
    except Exception as e:
        logger.error("session_remind.deliver_session_reminders(%s) failed: %s: %s", session_id, type(e).__name__, e)
    return result


async def deliver_morning_program(city: str, day: str) -> dict:
    """Тело утренней джобы города на день программы."""
    result = {"sent": 0, "skipped": 0}
    try:
        from services.forum.program import sessions_for_city_day
        from services.scheduler import _now_moscow_naive
        import domain.cities as _cities
        from database.db import list_approved_users

        if not await morning_enabled(city):
            return result
        sessions = await sessions_for_city_day(city, day)
        if not sessions:
            return result
        offset = await city_offset_hours(city)
        starts = [d for d in (session_start_msk(s, offset) for s in sessions) if d is not None]
        now = _now_moscow_naive()
        if not starts or now >= min(starts):
            return result  # день уже идёт: утренняя программа поздняя
        ref = f"{city}:{day}"
        tr_maps: dict = {}
        users = await _city_delegates(await list_approved_users(city_scope=_cities.city_scope(city)), city)
        for user in users:
            tid = int(user["telegram_id"])
            if await rdb.was_sent(tid, rdb.KIND_MORNING, ref):
                result["skipped"] += 1
                continue
            items = await morning_items_for_user(tid, city, day)
            if not items:
                continue
            lang, tr_map = await _lang_for(tid, tr_maps)
            text = await build_morning_text(city, items, lang=lang, tr_map=tr_map)
            if await _send(tid, text, rdb.KIND_MORNING, ref, now):
                result["sent"] += 1
            else:
                result["skipped"] += 1
            await asyncio.sleep(_THROTTLE_SECONDS)
    except Exception as e:
        logger.error("session_remind.deliver_morning_program(%s, %s) failed: %s: %s", city, day, type(e).__name__, e)
    return result


# ── Планирование ─────────────────────────────────────────────────────────────────────────────

def _remove_job(job_id: str) -> None:
    try:
        from services.scheduler import get_scheduler

        get_scheduler().remove_job(job_id)
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК


def cancel_for_session(session_id: int) -> None:
    _remove_job(remind_job_id(session_id))


def _job_ids(prefix: str) -> list[str]:
    try:
        from services.scheduler import get_scheduler

        return [j.id for j in get_scheduler().get_jobs() if j.id.startswith(prefix)]
    except Exception:
        return []


def _add_date_job(func, run_at: datetime, deadline: datetime, args: list, job_id: str) -> bool:
    """Ставит date-джобу на `run_at`; момент в прошлом, но до `deadline` — почти сейчас (догон),
    после дедлайна — не ставит. `misfire_grace_time` не даёт джобе сработать после дедлайна."""
    from services.scheduler import _now_moscow_naive, get_scheduler

    now = _now_moscow_naive()
    if now >= deadline:
        _remove_job(job_id)
        return False
    if run_at <= now:
        run_at = now + timedelta(minutes=1)
        if run_at >= deadline:
            run_at = now + timedelta(seconds=5)
    grace = max(30, int((deadline - run_at).total_seconds()))
    try:
        get_scheduler().add_job(
            func, "date", run_date=run_at, args=args, id=job_id, replace_existing=True,
            misfire_grace_time=grace,
        )
        return True
    except Exception as e:
        logger.warning("session_remind: не поставилась джоба %s: %s: %s", job_id, type(e).__name__, e)
        return False


async def schedule_for_session(session_id: int) -> bool:
    """Ставит/переставляет напоминание сессии — после любого создания/правки сессии, смены
    настроек, на старте. Выключено, сессия удалена, момент прошёл — джоба снимается."""
    from database.db import get_program_session

    session = await get_program_session(session_id)
    if session is None:
        cancel_for_session(session_id)
        return False
    city = session["city"]
    if not await remind_enabled(city):
        cancel_for_session(session_id)
        return False
    offset = await city_offset_hours(city)
    run_at = remind_run_at(session, await remind_minutes(city), offset)
    start = session_start_msk(session, offset)
    if run_at is None or start is None:
        return False
    return _add_date_job(deliver_session_reminders, run_at, start, [int(session_id)], remind_job_id(session_id))


async def on_session_changed(session_id: int, city: str | None = None) -> None:
    """Сессию создали/поправили/удалили (`handlers/forum/admin_program.py`): переставить её
    напоминание и утренние джобы города (день мог поменяться или опустеть). Fail-soft."""
    try:
        from database.db import get_program_session

        session = await get_program_session(session_id)
        if session is None:
            cancel_for_session(session_id)
        else:
            city = session["city"]
            await schedule_for_session(session_id)
        if city:
            await schedule_city_mornings(city)
    except Exception as e:
        logger.error("session_remind.on_session_changed(%s) failed: %s: %s", session_id, type(e).__name__, e)


async def schedule_city_mornings(city: str) -> int:
    """Переставляет утренние джобы города по всем дням программы; лишние снимает. Возвращает число
    поставленных."""
    from services.forum.program import sessions_for_city_day

    wanted: set[str] = set()
    placed = 0
    if await morning_enabled(city):
        offset = await city_offset_hours(city)
        hhmm = await morning_time(city)
        for day in await _program_days(city):
            run_at = morning_run_at(day, hhmm, offset)
            starts = [
                d for d in (session_start_msk(s, offset) for s in await sessions_for_city_day(city, day))
                if d is not None
            ]
            if run_at is None or not starts:
                continue
            job_id = morning_job_id(city, day)
            wanted.add(job_id)
            if _add_date_job(deliver_morning_program, run_at, min(starts), [city, day], job_id):
                placed += 1
    for job_id in _job_ids(f"{_MORNING_PREFIX}{city}:"):
        if job_id not in wanted:
            _remove_job(job_id)
    return placed


async def _program_days(city: str) -> list[str]:
    from database.db import list_program_days_for_city

    return await list_program_days_for_city(city)


async def reconcile_city(city: str) -> None:
    """Все джобы города: напоминания по сессиям + утренние по дням. Fail-soft."""
    try:
        from database.db import list_all_program_sessions

        for session in await list_all_program_sessions():
            if session.get("city") == city:
                await schedule_for_session(int(session["id"]))
        await schedule_city_mornings(city)
    except Exception as e:
        logger.error("session_remind.reconcile_city(%s) failed: %s: %s", city, type(e).__name__, e)


async def schedule_city_job(city: str | None) -> None:
    """Имя под `services/settings/reschedule._SPECS` (функция одного города)."""
    if city:
        await reconcile_city(city)
    else:
        await reconcile()


async def reconcile() -> None:
    """Старт бота / правка общего ключа: перевзвод всего и снятие джоб удалённых сессий."""
    try:
        from database.db import list_all_program_sessions

        sessions = await list_all_program_sessions()
        alive = {remind_job_id(s["id"]) for s in sessions}
        for job_id in _job_ids(_REMIND_PREFIX):
            if job_id not in alive:
                _remove_job(job_id)
        for session in sessions:
            await schedule_for_session(int(session["id"]))
        for city in sorted({s["city"] for s in sessions}):
            await schedule_city_mornings(city)
        for job_id in _job_ids(_MORNING_PREFIX):
            city = job_id[len(_MORNING_PREFIX):].split(":", 1)[0]
            if city not in {s["city"] for s in sessions}:
                _remove_job(job_id)
    except Exception as e:
        logger.error("session_remind.reconcile failed: %s: %s", type(e).__name__, e)


# ── Для экрана менеджера ─────────────────────────────────────────────────────────────────────

async def planned_summary(city: str) -> dict:
    """Что запланировано на ближайший день программы города: {"day", "reminders", "morning"}.
    Ближайший — первый день, в котором ещё есть не начавшиеся сессии (по городу)."""
    from services.forum.program import sessions_for_city_day
    from services.infra.timeutil import city_now

    now_local = await city_now(city)
    for day in await _program_days(city):
        sessions = await sessions_for_city_day(city, day)
        upcoming = [
            s for s in sessions
            if (_local_dt(s["day"], s["start_time"]) or datetime.min) > now_local
        ]
        if not upcoming:
            continue
        reminders = 0
        if await remind_enabled(city):
            planned = set(_job_ids(_REMIND_PREFIX))
            reminders = sum(1 for s in upcoming if remind_job_id(s["id"]) in planned)
        morning = (
            await morning_enabled(city) and morning_job_id(city, day) in set(_job_ids(_MORNING_PREFIX))
        )
        return {"day": day, "reminders": reminders, "sessions": len(upcoming), "morning": morning}
    return {"day": None, "reminders": 0, "sessions": 0, "morning": False}


async def build_examples(city: str, telegram_id: int) -> tuple[str, str] | None:
    """Пример для менеджера на реальных данных: (напоминание о ближайшей сессии, программа дня).
    None — в программе города нет ни одной сессии."""
    from services.forum.program import sessions_for_city_day
    from services.infra.timeutil import city_now

    days = await _program_days(city)
    if not days:
        return None
    now_local = await city_now(city)
    target_session = None
    target_day = None
    for day in days:
        sessions = await sessions_for_city_day(city, day)
        for s in sessions:
            if (_local_dt(s["day"], s["start_time"]) or datetime.min) > now_local:
                target_session, target_day = s, day
                break
        if target_session:
            break
    if target_session is None:
        target_day = days[0]
        sessions = await sessions_for_city_day(city, target_day)
        if not sessions:
            return None
        target_session = sessions[0]
    remind = await build_remind_text(target_session, await remind_minutes(city))
    items = await morning_items_for_user(telegram_id, city, target_day)
    if not items:
        sessions = await sessions_for_city_day(city, target_day)
        items = [{"session": s, "kind": "chosen"} for s in sessions]
    return remind, await build_morning_text(city, items)
