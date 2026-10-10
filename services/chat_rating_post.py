"""Квик 260927: еженедельный пост рейтинга в чат делегатов города.

Организатор СПб обещал делегатам «каждую неделю таблица самых богатых участников форума» —
бот публикует её сам в привязанный чат города (`services.chat_tracking.chat_for_city`).

Что в посте. Период — последняя ЗАВЕРШЁННАЯ неделя пн–вс по Москве (пост в воскресенье вечером
тоже покажет прошлую, а не идущую неделю: итог недели публикуется, когда она закончилась).
По желанию — вторая таблица «с начала» (с начала истории по конец той же недели). Режим города:
«по правилам» — итог в валюте города («LC», «коины»), «по формуле» — балл активности.
Расчёт ОБЩИЙ с дашбордом — `dashboard/chat_rating.py` (пакет dashboard/ есть в образе бота:
корневой Dockerfile копирует репозиторий целиком), второй копии формулы здесь нет. Команда
(staff + ADMIN_IDS + админы группы) исключена там же.

Упоминания — только @ником (Telegram-ник из журнала чата, иначе ник из анкеты — та же лестница,
что `chat_rating.display_names`). ФИО в группу не пишем никогда; имени из чата бот не хранит,
поэтому человек без ника в пост не попадает (место отдаётся следующему). @ник в группе
присылает человеку уведомление — это и есть смысл поста. Нулевые строки не показываются;
пустая неделя — поста нет (пишем в лог).

Тихие часы и «🔕 Не беспокоить» не действуют: это одно сообщение в группу, а не личная
рассылка делегатам.

Планирование — одна cron-джоба на город в персистентном jobstore (`chat_rating_post:{код}`),
переставляется сразу при правке на экране и сверкой раз в 10 минут (настройки правит и Mini App,
где планировщика нет). Джоба перед отправкой перечитывает тумблер, привязку и все настройки.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, timedelta

from cities import cities_module_on, enabled_cities, get_setting_typed_for_city, per_city_key
from config import config
from database.db import get_setting
from services.timeutil import MOSCOW_TZ, msk_now
from domain.settings.schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

KEY_ENABLED = "chat_rating_post_enabled"
KEY_WEEKDAY = "chat_rating_post_weekday"
KEY_TIME = "chat_rating_post_time"
KEY_TOP = "chat_rating_post_top"
KEY_CUMULATIVE = "chat_rating_post_cumulative"
KEY_TITLE_RULES = "chat_rating_post_title_rules"
KEY_TITLE_FORMULA = "chat_rating_post_title_formula"
KEY_TOTAL_TITLE = "chat_rating_post_total_title"
KEY_FOOTER = "chat_rating_post_footer"

WEEKDAYS: dict = dict(SETTINGS_SCHEMA[KEY_WEEKDAY]["option_labels"])
TOP_MIN, TOP_MAX = 1, 30  # 30 строк — пост заведомо влезает в 4096 символов
DEFAULT_TIME = SETTINGS_SCHEMA[KEY_TIME]["default"]

_JOB_PREFIX = "chat_rating_post:"
_MEDALS = ("🥇", "🥈", "🥉")


def job_id(city: str | None) -> str:
    return f"{_JOB_PREFIX}{city or 'all'}"


# ── Чистые функции ──────────────────────────────────────────────────────────────────────

def last_week(today: date) -> tuple[date, date]:
    """Последняя завершённая неделя пн–вс относительно `today` (дата по Москве)."""
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def week_label(since: date, until: date) -> str:
    return f"{since:%d.%m}–{until:%d.%m}"


def parse_time(raw) -> tuple[int, int]:
    text = str(raw or DEFAULT_TIME).strip()
    try:
        hh, mm = (int(x) for x in text.split(":", 1))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except (TypeError, ValueError):
        pass
    hh, mm = DEFAULT_TIME.split(":")
    return int(hh), int(mm)


def clamp_top(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = SETTINGS_SCHEMA[KEY_TOP]["default"]
    return max(TOP_MIN, min(TOP_MAX, value))


def _num(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _fill(template: str, week: str, currency: str) -> str:
    """Текст менеджера экранируется целиком, плейсхолдеры подставляются заменой строки —
    случайные фигурные скобки в тексте не роняют пост (в отличие от str.format)."""
    out = html.escape(template or "")
    return out.replace("{week}", html.escape(week)).replace("{currency}", html.escape(currency))


def _lines(rows, top: int, currency: str) -> list[str]:
    kept = [
        r for r in rows or ()
        if str(r.get("display_name") or "").startswith("@") and (r.get("value") or 0) > 0
    ][:top]
    suffix = f" {html.escape(currency)}" if currency else ""
    out = []
    for place, row in enumerate(kept, start=1):
        mark = _MEDALS[place - 1] if place <= len(_MEDALS) else f"{place}."
        out.append(f"{mark} {html.escape(row['display_name'])} — {_num(row['value'])}{suffix}")
    return out


def format_post(*, week_rows, total_rows, top: int, currency: str, title: str,
                total_title: str, footer: str, week: str) -> str | None:
    """Текст поста (HTML) или None, если за неделю показать некого."""
    week_lines = _lines(week_rows, top, currency)
    if not week_lines:
        return None
    parts = [f"<b>{_fill(title, week, currency)}</b>", "\n".join(week_lines)]
    total_lines = _lines(total_rows, top, currency) if total_rows is not None else []
    if total_lines:
        parts.append(f"<b>{_fill(total_title, week, currency)}</b>\n" + "\n".join(total_lines))
    if (footer or "").strip():
        parts.append(_fill(footer.strip(), week, currency))
    return "\n\n".join(parts)


# ── Настройки города ────────────────────────────────────────────────────────────────────

async def enabled_for(city: str | None) -> bool:
    """Тумблер строго по городу, когда модуль городов включён: общий «вкл» не должен молча
    включить пост во всех чатах сразу. Модуль выключен — общий ключ."""
    if await cities_module_on():
        key = per_city_key(KEY_ENABLED, city) if city else None
        if key is None:
            return False
        raw = await get_setting(key)
    else:
        raw = await get_setting(KEY_ENABLED)
    return (raw or "").strip() == "on"


async def post_settings(city: str | None) -> dict:
    async def typed(key):
        return await get_setting_typed_for_city(key, city)

    weekday = await typed(KEY_WEEKDAY)
    return {
        "weekday": weekday if weekday in WEEKDAYS else SETTINGS_SCHEMA[KEY_WEEKDAY]["default"],
        "time": parse_time(await typed(KEY_TIME)),
        "top": clamp_top(await typed(KEY_TOP)),
        "cumulative": await typed(KEY_CUMULATIVE) == "on",
        "title_rules": await typed(KEY_TITLE_RULES) or "",
        "title_formula": await typed(KEY_TITLE_FORMULA) or "",
        "total_title": await typed(KEY_TOTAL_TITLE) or "",
        "footer": await typed(KEY_FOOTER) or "",
    }


# ── Расчёт (общий с дашбордом) ──────────────────────────────────────────────────────────

def _compute(db_path: str, chat: dict, now: datetime, cumulative: bool, admin_ids) -> dict:
    """Синхронно (sqlite3 mode=ro) — зовётся через asyncio.to_thread."""
    from dashboard import chat_rating
    from dashboard.db import read_conn

    since, until = last_week(now.date())
    target = {"chat_id": chat["chat_id"], "city": chat.get("city")}
    with read_conn(db_path) as conn:
        mode = chat_rating.chat_mode(conn, target)
        rating = chat_rating.rules_rating if mode == "rules" else chat_rating.chat_rating
        week = rating(conn, target, period="prev_week", admin_ids=admin_ids, now=now,
                      bounds=(since, until))
        total = None
        if cumulative:
            total = rating(conn, target, period="all", admin_ids=admin_ids, now=now,
                           bounds=(None, until))
    value_key = "total" if mode == "rules" else "score"

    def rows(result):
        if result is None:
            return None
        return [{"display_name": r["display_name"], "value": r[value_key]} for r in result["rows"]]

    return {
        "mode": mode,
        "week": week_label(since, until),
        "currency": week.get("currency", "") if mode == "rules" else "",
        "week_rows": rows(week),
        "total_rows": rows(total),
    }


async def build_post(city: str | None, *, now: datetime | None = None) -> tuple:
    """(текст или None, привязанный чат или None). Текст None — за неделю показать некого."""
    from services.chat_tracking import chat_for_city

    chat = await chat_for_city(city)
    if chat is None:
        return None, None
    cfg = await post_settings(city)
    data = await asyncio.to_thread(
        _compute, config.DB_PATH, chat, now or msk_now(), cfg["cumulative"],
        list(config.ADMIN_IDS or ()),
    )
    title = cfg["title_rules"] if data["mode"] == "rules" else cfg["title_formula"]
    text = format_post(
        week_rows=data["week_rows"], total_rows=data["total_rows"], top=cfg["top"],
        currency=data["currency"], title=title, total_title=cfg["total_title"],
        footer=cfg["footer"], week=data["week"],
    )
    return text, chat


def week_key(now: datetime) -> str:
    """Ключ недели поста — дата понедельника последней завершённой недели."""
    since, _until = last_week(now.date())
    return since.isoformat()


async def publish(city: str | None, bot, *, now: datetime | None = None) -> tuple[str, dict | None]:
    """Отправить пост в чат города. Статус: ok / not_bound / empty / send_failed. Удачный пост
    запоминает неделю (`chat_rating_posts`) — плановая джоба ту же неделю второй раз не шлёт."""
    now = now or msk_now()
    text, chat = await build_post(city, now=now)
    if chat is None:
        return "not_bound", None
    if text is None:
        return "empty", chat
    try:
        await bot.send_message(chat["chat_id"], text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"chat_rating_post: не удалось отправить в чат {chat['chat_id']} "
                       f"(город {city!r}): {e}")
        return "send_failed", chat
    try:
        from database.db import set_chat_rating_posted_week
        await set_chat_rating_posted_week(city, week_key(now))
    except Exception as e:
        logger.error(f"chat_rating_post: неделя поста не запомнена (город {city!r}): {e}")
    return "ok", chat


# ── Планирование ────────────────────────────────────────────────────────────────────────

async def _city_still_valid(city: str | None) -> bool:
    if city is not None and await cities_module_on():
        if city not in {c["code"] for c in await enabled_cities()}:
            return False
    return await enabled_for(city)


def cancel_city_job(city: str | None) -> None:
    from services.scheduler import get_scheduler

    try:
        get_scheduler().remove_job(job_id(city))
    except Exception:
        pass  # не стояла — нормально


async def schedule_city_job(city: str | None) -> dict:
    """(Пере)ставить cron-джобу города или снять её (тумблер выключен / город выключен).
    Тот же день и время — джоба не переписывается (сверка раз в 10 минут её не дёргает)."""
    from apscheduler.triggers.cron import CronTrigger

    from services.scheduler import get_scheduler

    sched = get_scheduler()
    jid = job_id(city)
    if not await _city_still_valid(city):
        cancel_city_job(city)
        return {"scheduled": False, "reason": "disabled"}
    cfg = await post_settings(city)
    hh, mm = cfg["time"]
    trigger = CronTrigger(day_of_week=cfg["weekday"], hour=hh, minute=mm, timezone=MOSCOW_TZ)
    existing = sched.get_job(jid)
    if existing is not None and str(existing.trigger) == str(trigger):
        return {"scheduled": True, "unchanged": True}
    sched.add_job(run_job, trigger, args=[city], id=jid, replace_existing=True)
    return {"scheduled": True, "unchanged": False}


async def reschedule_soft(city: str | None) -> None:
    """Для хендлеров экрана: планировщика может не быть (тесты, отдельный процесс) — правка
    настройки от этого не должна падать; джобу поставит сверка при старте бота."""
    try:
        await schedule_city_job(city)
    except Exception as e:
        logger.info(f"chat_rating_post: перестановка джобы города {city!r} отложена: {e}")


async def reconcile() -> None:
    """Старт бота и каждые 10 минут: джоба у каждого включённого города, лишние — снять."""
    from services.scheduler import get_scheduler

    try:
        codes = [c["code"] for c in await enabled_cities()] if await cities_module_on() else [None]
        for code in codes:
            try:
                await schedule_city_job(code)
            except Exception as e:
                logger.error(f"chat_rating_post.reconcile({code!r}) failed: {e}")
        keep = {job_id(code) for code in codes}
        sched = get_scheduler()
        for job in sched.get_jobs():
            if job.id.startswith(_JOB_PREFIX) and job.id not in keep:
                try:
                    sched.remove_job(job.id)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"chat_rating_post.reconcile failed: {e}")


async def run_job(city: str | None) -> None:
    """Цель cron-джобы: всё перечитывается в момент срабатывания."""
    try:
        if not await _city_still_valid(city):
            logger.info(f"chat_rating_post: город {city!r} — публикация выключена, пропуск")
            return
        from database.db import get_chat_rating_posted_week
        from services.scheduler import get_bot

        now = msk_now()
        # Смена дня/времени после сегодняшнего поста переставляет джобу — пост той же недели
        # второй раз не уходит (ручная публикация тоже считается).
        if await get_chat_rating_posted_week(city) == week_key(now):
            logger.info(f"chat_rating_post: город {city!r} — пост за эту неделю уже был, пропуск")
            return
        status, chat = await publish(city, get_bot(), now=now)
        if status == "ok":
            logger.info(f"chat_rating_post: рейтинг опубликован в чат {chat['chat_id']} ({city!r})")
        elif status == "not_bound":
            logger.info(f"chat_rating_post: город {city!r} — чат не привязан, пропуск")
        elif status == "empty":
            logger.info(f"chat_rating_post: город {city!r} — за неделю никого в рейтинге, пропуск")
    except Exception as e:
        logger.error(f"chat_rating_post.run_job({city!r}) failed: {e}")
