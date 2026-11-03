"""Правила записи на интервью: окно, кап, один перенос. Экранов здесь нет.

Планировщик подключается внутри функции, не при импорте: этот модуль не должен
тянуть aiogram, а джоба напоминания не должна импортировать этот файл наверху.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from database import interview_db
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

_YES = "Да"
_OPEN = ("pending", "approved", "waitlist")
_NOT_OPEN = "Запись ещё не открыта."
_SAME = "Это и есть твоё время."
_NEED_YES = "Записаться можно, если в анкете на кейс-чемпионат выбран ответ «Да»."
_BAD_STATUS = "С этой заявкой записаться на интервью нельзя."
_OTHER_SEASON = "Запись на интервью открыта для текущего сезона."
_OFF = "Запись на интервью сейчас выключена."
_ALREADY = "Ты уже выбрал время."
_GONE = "Этого времени уже нет."
_OTHER_CITY = "Это время открыто для другого города."


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _parse_start(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError, AttributeError):
        return None


async def _text(key: str, fallback: str) -> str:
    value = await get_setting_typed(key)
    return value if isinstance(value, str) and value.strip() else fallback


async def _yes_labels() -> set[str]:
    from domain.regform.engine import options

    labels = await options("case_optin")
    found = {_YES}
    if labels:
        found.add(str(labels[0]))
    return found


async def eligible(user: dict) -> str | None:
    """None — записаться можно. Иначе короткая фраза для участника."""
    if await get_setting_typed("interview_booking_enabled") != "on":
        return _OFF
    if (user.get("status") or "") not in _OPEN:
        return _BAD_STATUS
    answer = user.get("case_optin")
    if not isinstance(answer, str) or answer.strip() not in await _yes_labels():
        return _NEED_YES
    season = (user.get("season") or "").strip()
    current = await get_setting_typed("event_season")
    current = current.strip() if isinstance(current, str) else ""
    if season and season != current:
        return _OTHER_SEASON
    return None


async def window_open(now: datetime) -> str | None:
    """None — сейчас можно записываться. Пустая граница окно не открывает."""
    start = await get_setting_typed("interview_window_start")
    end = await get_setting_typed("interview_window_end")
    if not isinstance(start, datetime) or not isinstance(end, datetime) or now < start:
        return _NOT_OPEN
    if now > end:
        return await _text("interview_closed_text", "Запись закрыта.")
    return None


def _city_matches(user: dict, slot: dict) -> bool:
    """Общий слот (город пустой) открыт всем. Слот с кодом — только этому городу анкеты."""
    code = slot.get("event_city")
    if not code:
        return True
    return (user.get("event_city") or "").strip() == str(code).strip()


def _arm(user_id: int, starts_at: str, slot_id: int) -> None:
    starts = _parse_start(starts_at)
    if starts is None:
        return
    try:
        from services.scheduler import schedule_interview_reminder
        schedule_interview_reminder(user_id, slot_id, starts - timedelta(hours=2))
    except Exception:
        logger.warning("interview reminder was not scheduled for %s", user_id)


async def book_slot(user: dict, slot_id: int, now: datetime | None = None) -> str | None:
    reason = await eligible(user)
    if reason:
        return reason
    from services.infra.timeutil import msk_now
    moment = now if now is not None else msk_now()
    closed = await window_open(moment)
    if closed:
        return closed
    slot = await interview_db.get_slot(slot_id)
    if slot is None:
        return _GONE
    if not _city_matches(user, slot):
        return _OTHER_CITY
    code = await interview_db.book(int(user["telegram_id"]), int(slot_id), _stamp(moment))
    if code == "full":
        return await _text("interview_full_text", "Это время уже занято.")
    if code == "exists":
        return _ALREADY
    if code != "ok":
        return _GONE
    _arm(int(user["telegram_id"]), slot["starts_at"], int(slot_id))
    return None


async def move_slot(user: dict, slot_id: int, now: datetime | None = None) -> str | None:
    reason = await eligible(user)
    if reason:
        return reason
    from services.infra.timeutil import msk_now
    moment = now if now is not None else msk_now()
    closed = await window_open(moment)
    if closed:
        return closed
    uid = int(user["telegram_id"])
    booking = await interview_db.get_booking(uid)
    if booking is None:
        return _GONE
    if int(booking["slot_id"]) == int(slot_id):
        return _SAME
    if int(booking["reschedule_count"]) != 0:
        return await _text("interview_reschedule_spent_text", "Перенести можно было один раз.")
    slot = await interview_db.get_slot(slot_id)
    if slot is None:
        return _GONE
    if not _city_matches(user, slot):
        return _OTHER_CITY
    code = await interview_db.reschedule(uid, int(slot_id), _stamp(moment))
    if code == "full":
        return await _text("interview_full_text", "Это время уже занято.")
    if code == "same":
        return _SAME
    if code != "ok":
        return await _text("interview_reschedule_spent_text", "Перенести можно было один раз.")
    _arm(uid, slot["starts_at"], int(slot_id))
    return None
