"""Файл календаря из даты форума города.

Даты нет — файла нет, одобрение из-за этого не ломается. Конец дня — 23:59,
если менеджер не задал второе время. Список вещей и тексты здесь не придуманы.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_FALLBACK_START = "10:00"
_FALLBACK_END = "23:59"
_FALLBACK_TZ = "Europe/Moscow"
_FILENAME = "forum.ics"


def _clock(raw, fallback: str) -> time:
    text = (str(raw).strip() if raw else "") or fallback
    try:
        return datetime.strptime(text, "%H:%M").time().replace(second=0)
    except ValueError:
        return datetime.strptime(fallback, "%H:%M").time()


def _zone(name) -> ZoneInfo:
    text = (str(name).strip() if name else "") or _FALLBACK_TZ
    try:
        return ZoneInfo(text)
    except Exception:
        return ZoneInfo(_FALLBACK_TZ)


def _ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", r"\;")
        .replace(",", r"\,")
    )


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(
    *, day, start: time, end: time, tz_name: str, summary: str, location: str, uid: str,
) -> bytes:
    """Один VEVENT. Время города переводится в UTC, чтобы телефон не гадал пояс."""
    zone = _zone(tz_name)
    start_at = datetime.combine(day, start, tzinfo=zone)
    end_at = datetime.combine(day, end, tzinfo=zone)
    if end_at <= start_at:
        end_at = datetime.combine(day, _clock(None, _FALLBACK_END), tzinfo=zone)
    now = _utc_stamp(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AIESEC Event Bot//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_utc_stamp(start_at)}",
        f"DTEND:{_utc_stamp(end_at)}",
        f"SUMMARY:{_ics_text(summary)}",
        f"LOCATION:{_ics_text(location)}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


async def _location(city_code: str | None) -> str:
    if city_code:
        from domain.cities import cities_module_on, city_label
        if await cities_module_on():
            try:
                label = (await city_label(city_code) or "").strip()
                if label:
                    return label
            except Exception:
                logger.exception("календарь: нет названия города %s", city_code)
    from domain.settings.schema import get_setting_typed
    place = (await get_setting_typed("event_place_name") or "").strip()
    if place:
        return place
    name = (await get_setting_typed("event_name") or "").strip()
    return name or "Форум"


async def calendar_for_city(city_code: str | None) -> tuple[bytes, str] | None:
    """Байты .ics и имя файла. None — у города нет даты форума."""
    from domain.cities import get_setting_typed_for_city
    from domain.settings.schema import get_setting_typed
    from services.applications.reject_rules import forum_date_for

    raw_date = await forum_date_for(city_code)
    if not raw_date:
        return None
    try:
        day = datetime.strptime(raw_date, "%d.%m.%Y").date()
    except ValueError:
        return None
    start = _clock(await get_setting_typed_for_city("forum_time", city_code), _FALLBACK_START)
    end_raw = await get_setting_typed_for_city("forum_end_time", city_code)
    end = _clock(end_raw, _FALLBACK_END) if end_raw else _clock(None, _FALLBACK_END)
    tz_name = await get_setting_typed_for_city("forum_timezone", city_code) or _FALLBACK_TZ
    summary = (await get_setting_typed("event_name") or "").strip() or "Форум"
    location = await _location(city_code)
    slug = city_code or "all"
    uid = f"forum-{slug}-{day.strftime('%Y%m%d')}@aiesec"
    return build_ics(
        day=day, start=start, end=end, tz_name=str(tz_name),
        summary=summary, location=location, uid=uid,
    ), _FILENAME


async def send_forum_calendar(bot, telegram_id: int, city_code: str | None) -> None:
    """Вложение к письму об одобрении. Нет даты или сбой отправки — тишина, письмо уже ушло."""
    try:
        built = await calendar_for_city(city_code)
        if built is None:
            return
        data, filename = built
        from aiogram.types import BufferedInputFile
        await bot.send_document(telegram_id, BufferedInputFile(data, filename=filename))
    except Exception:
        logger.exception("календарь: не ушло %s", telegram_id)
