"""Правила записи делегата на сессии программы — единая точка для чата, сканера и админки.

Модуль не знает про aiogram: хендлеры чата (кнопки), эндпоинт сканера Mini App и экран
админки зовут одни и те же функции, поэтому правила не расходятся между поверхностями.

Пересечение для записи ПОПАРНОЕ (`overlaps`, `enroll_tx`): A 10:00-11:00 и C 10:30-12:00
конфликтуют, а A с D 11:30-12:30 — нет. Это НЕ `parallel_group` / `slot_other_ids` чек-ина:
те строят транзитивный слот. Слоты для ПОКАЗА выбора (`slots_for_city`) группируются
`group_parallel` и только по сессиям с треком — пленарки без трека в них не входят, но есть
в «Моём расписании» у всех.

Текстов для людей здесь нет: результат несёт `text_key` — ключ реестра настроек, который
хендлер читает per_city и подставляет значения.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from cities import get_setting_typed_for_city, normalize_city
from database import session_enroll_db as _edb
from database.db import get_program_hall, get_program_session, get_user, list_program_days_for_city
from services.checkin import checkin_denial
from services.program import group_parallel, sessions_for_city_day
from services.timeutil import city_now

logger = logging.getLogger(__name__)

# Все текстовые ключи модуля записи (для экрана «✏️ Тексты» в админке), порядок как в реестре.
ENROLL_TEXT_KEYS: tuple[str, ...] = (
    "session_enroll_intro_text",
    "session_enroll_mix_button",
    "session_enroll_slot_text",
    "session_enroll_next_button",
    "session_enroll_clear_button",
    "session_enroll_closed_label",
    "session_enroll_full_label",
    "session_enroll_schedule_title",
    "session_enroll_schedule_empty",
    "session_enroll_common_label",
    "session_enroll_change_button",
    "session_enroll_confirm_button",
    "session_enroll_confirmed_text",
    "session_enroll_replace_question",
    "session_enroll_replace_yes",
    "session_enroll_replace_no",
    "session_enroll_err_closed",
    "session_enroll_err_full",
    "session_enroll_err_deadline",
    "session_enroll_not_approved_text",
    "session_enroll_disabled_text",
    "session_enroll_no_sessions_text",
    "session_enroll_scan_other_text",
    "session_enroll_scan_none_text",
    "session_enroll_scan_rebook_button",
    "session_enroll_scan_book_button",
    "session_enroll_scan_done_text",
)

_DEADLINE_FMT = "%d.%m.%Y %H:%M"


@dataclass
class EnrollOutcome:
    """status: ok | already | conflict | closed | full | deadline | disabled | not_enrollable |
    wrong_city | no_session | denied. `text_key` — ключ реестра для ответа; `denial` — код
    отказа допуска (`checkin_denial`), чтобы чат мог показать баннер возвращенца."""
    status: str
    session: dict | None = None
    conflicts: list[dict] = field(default_factory=list)
    replaced: list[dict] = field(default_factory=list)
    text_key: str | None = None
    denial: str | None = None


def overlaps(a: dict, b: dict) -> bool:
    """Попарное пересечение двух сессий: один день и `a.start < b.end AND b.start < a.end`."""
    if a.get("day") != b.get("day"):
        return False
    return a["start_time"] < b["end_time"] and b["start_time"] < a["end_time"]


# ── Модуль и дедлайн ────────────────────────────────────────────────────────────────────────

async def module_enabled(city: str | None) -> bool:
    try:
        return await get_setting_typed_for_city("session_enroll_enabled", city) == "on"
    except Exception as e:
        logger.warning("session_enroll_enabled(%s) не прочитан: %s", city, e)
        return False


async def deadline_for(city: str | None) -> datetime | None:
    """Дедлайн записи (местное время города) или None. Мусор в настройке — «нет дедлайна»."""
    try:
        raw = await get_setting_typed_for_city("session_enroll_deadline", city)
    except Exception as e:
        logger.warning("session_enroll_deadline(%s) не прочитан: %s", city, e)
        return None
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.strptime(str(raw).strip(), _DEADLINE_FMT)
    except ValueError:
        logger.warning("session_enroll_deadline(%s) не разобран: %r", city, raw)
        return None


async def deadline_passed(city: str | None) -> bool:
    deadline = await deadline_for(city)
    if deadline is None:
        return False
    return await city_now(city) >= deadline


async def deadline_label(city: str | None) -> str:
    deadline = await deadline_for(city)
    return deadline.strftime(_DEADLINE_FMT) if deadline else ""


# ── Состояние сессии ────────────────────────────────────────────────────────────────────────

async def session_open_state(session: dict, count: int | None = None) -> str:
    """"open" | "closed" | "full"."""
    if session.get("enroll_closed"):
        return "closed"
    limit = session.get("enroll_limit")
    if limit is not None:
        taken = count if count is not None else await _edb.count_enrollments(session["id"])
        if taken >= int(limit):
            return "full"
    return "open"


async def _sessions_by_ids(ids: list[int]) -> list[dict]:
    out = []
    for sid in ids:
        s = await get_program_session(sid)
        if s:
            out.append(s)
    return out


# ── Запись ──────────────────────────────────────────────────────────────────────────────────

async def enroll(telegram_id: int, session_id: int, *, confirm_replace: bool = False) -> EnrollOutcome:
    """Самостоятельная запись. Все гейты перепроверяются на КАЖДЫЙ вызов — устаревшая кнопка
    ничего не обходит."""
    session = await get_program_session(session_id)
    if not session:
        return EnrollOutcome("no_session")
    city = session["city"]
    user = await get_user(telegram_id)
    denial = await checkin_denial(user)
    if denial:
        return EnrollOutcome("denied", session=session, text_key="session_enroll_not_approved_text",
                             denial=denial)
    if not await module_enabled(city):
        return EnrollOutcome("disabled", session=session, text_key="session_enroll_disabled_text")
    if normalize_city(user.get("event_city")) != city:
        return EnrollOutcome("wrong_city", session=session)
    if session.get("track_id") is None:
        return EnrollOutcome("not_enrollable", session=session)
    if await deadline_passed(city):
        return EnrollOutcome("deadline", session=session, text_key="session_enroll_err_deadline")
    state = await session_open_state(session)
    if state == "closed":
        return EnrollOutcome("closed", session=session, text_key="session_enroll_err_closed")
    if state == "full":
        return EnrollOutcome("full", session=session, text_key="session_enroll_err_full")

    res = await _edb.enroll_tx(telegram_id, session_id, allow_replace=confirm_replace,
                               limit_check=True, source="self")
    return await _outcome_from_tx(res, session)


async def _outcome_from_tx(res, session: dict) -> EnrollOutcome:
    if res.status == "ok":
        return EnrollOutcome("ok", session=session, replaced=await _sessions_by_ids(res.replaced))
    if res.status == "conflict":
        return EnrollOutcome("conflict", session=session,
                             conflicts=await _sessions_by_ids(res.conflicts))
    if res.status == "full":
        return EnrollOutcome("full", session=session, text_key="session_enroll_err_full")
    return EnrollOutcome(res.status, session=session)


async def enroll_by_staff(telegram_id: int, session_id: int, *, by_staff_id: int) -> EnrollOutcome:
    """Запись волонтёром на месте (сканер). Права НЕ проверяет — вызывающий эндпоинт обязан
    быть под `require_cap("checkin")` с привязкой города. Игнорирует закрытие, лимит и дедлайн;
    пересекающиеся записи заменяет."""
    session = await get_program_session(session_id)
    if not session:
        return EnrollOutcome("no_session")
    if not await module_enabled(session["city"]):
        return EnrollOutcome("disabled", session=session, text_key="session_enroll_disabled_text")
    if session.get("track_id") is None:
        return EnrollOutcome("not_enrollable", session=session)
    res = await _edb.enroll_tx(telegram_id, session_id, allow_replace=True, limit_check=False,
                               source="scan", by_staff_id=by_staff_id)
    return await _outcome_from_tx(res, session)


async def unenroll(telegram_id: int, session_id: int) -> EnrollOutcome:
    session = await get_program_session(session_id)
    if not session:
        return EnrollOutcome("no_session")
    city = session["city"]
    if not await module_enabled(city):
        return EnrollOutcome("disabled", session=session, text_key="session_enroll_disabled_text")
    if await deadline_passed(city):
        return EnrollOutcome("deadline", session=session, text_key="session_enroll_err_deadline")
    if session.get("enroll_closed"):
        return EnrollOutcome("closed", session=session, text_key="session_enroll_err_closed")
    await _edb.unenroll(telegram_id, session_id)
    return EnrollOutcome("ok", session=session)


# ── Показ ───────────────────────────────────────────────────────────────────────────────────

async def slots_for_city(city: str) -> list[dict]:
    """[{day, slots: [[session, ...], ...]}] — только сессии с треком; у каждой track_name,
    hall_name, enrolled, open_state."""
    sessions = await _edb.list_trackable_sessions(city)
    counts = await _edb.enrollment_counts_for_city(city)
    halls: dict[int, str | None] = {}
    days: dict[str, list[dict]] = {}
    for s in sessions:
        hid = s.get("hall_id")
        if hid is not None and hid not in halls:
            hall = await get_program_hall(hid)
            halls[hid] = hall["name"] if hall else None
        enrolled = counts.get(s["id"], 0)
        item = {**s, "hall_name": halls.get(hid), "enrolled": enrolled,
                "open_state": await session_open_state(s, enrolled)}
        days.setdefault(s["day"], []).append(item)
    return [{"day": day, "slots": group_parallel(items)} for day, items in sorted(days.items())]


async def my_schedule(telegram_id: int, city: str) -> list[dict]:
    """[{day, items: [{session, kind}]}]: kind "common" (без трека) | "chosen"."""
    chosen = await _edb.list_user_enrollments(telegram_id, city)
    chosen_ids = {s["id"] for s in chosen}
    by_day: dict[str, list[dict]] = {}
    for s in chosen:
        by_day.setdefault(s["day"], []).append({"session": s, "kind": "chosen"})
    for day in await list_program_days_for_city(city):
        for s in await sessions_for_city_day(city, day):
            if s.get("track_id") is None and s["id"] not in chosen_ids:
                by_day.setdefault(day, []).append({"session": s, "kind": "common"})
    result = []
    for day in sorted(by_day):
        items = sorted(by_day[day], key=lambda i: (i["session"]["start_time"], i["session"]["id"]))
        result.append({"day": day, "items": items})
    return result


async def confirm(telegram_id: int, city: str) -> str:
    return await _edb.confirm_schedule(telegram_id, city)


# ── Подсказка сканера ───────────────────────────────────────────────────────────────────────

async def scan_hint(user: dict, session_id: int) -> dict | None:
    """Подсказка волонтёру на скане точки-сессии: `{"hint", "enroll": {action, session_id,
    label}}` или None. Fail-soft: сбой не должен ронять скан."""
    try:
        session = await get_program_session(session_id)
        if not session or session.get("track_id") is None:
            return None
        city = session["city"]
        if not await module_enabled(city):
            return None
        mine = [s for s in await _edb.list_user_enrollments(user["telegram_id"], city)
                if s["day"] == session["day"]]
        if any(s["id"] == session["id"] for s in mine):
            return None
        clash = [s for s in mine if overlaps(s, session)]
        if clash:
            template = await get_setting_typed_for_city("session_enroll_scan_other_text", city)
            hint = (template or "").replace("{title}", clash[0].get("title") or "")
            action, label_key = "rebook", "session_enroll_scan_rebook_button"
        else:
            hint = await get_setting_typed_for_city("session_enroll_scan_none_text", city)
            action, label_key = "book", "session_enroll_scan_book_button"
        label = await get_setting_typed_for_city(label_key, city)
        return {"hint": hint, "enroll": {"action": action, "session_id": session["id"],
                                         "label": label}}
    except Exception:
        logger.exception("scan_hint(%s) не отработал", session_id)
        return None
