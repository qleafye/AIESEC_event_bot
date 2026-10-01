"""Отметка входа только в день форума города делегата.

Окно форума города — `[forum_date, forum_date + sos_active_days - 1]` (тот же расчёт, что меню
дня форума, SOS и отчёт дня: `services.forum_day_menu._forum_window_dates`). Дата форума не
задана — проверки нет (как раньше).

Зачем: накануне волонтёр получает шпаргалку (D-33), открывает сканер и пробует его на QR
друга-делегата, не переключившись на «🧪 Тренировка». Раньше это ставило настоящий вход: в
листе «Пришёл» — накануне, делегату уходило «ты отмечен», а утром настоящего приветствия уже
не было (первый вход за форум прошёл). Теперь живой скан и отметка по фамилии в не-день форума
не пишутся — волонтёр видит жёлтую плашку с подсказкой. Та же проверка закрывает волонтёра без
привязки к городу: 03.10 на входе в СПб одобренный делегат Москвы (форум 30.10) не отмечается
молча зелёным — плашка говорит, что у него форум не сегодня. Выгрузка офлайн-сканера (CSV) не
отказывает — файл описывает уже случившееся, — а предупреждает строкой отчёта.

Если сегодня форум идёт в нескольких городах, успешная отметка у волонтёра без привязки
показывает город делегата крупно (`city_emphasis`)."""
from __future__ import annotations

import logging
from datetime import date, datetime

from cities import cities_module_on, city_label, default_city_code, enabled_cities, normalize_city

logger = logging.getLogger(__name__)

STATUS = "not_forum_day"


async def forum_window(city: str | None) -> tuple[date, date] | None:
    from services.forum_day_menu import _forum_window_dates  # тот же расчёт окна, что меню дня
    try:
        return await _forum_window_dates(city)
    except Exception:  # noqa: BLE001 — сбой чтения настройки не должен ронять отметку
        logger.exception("checkin_forum_day: не прочитал окно форума города %r", city)
        return None


def _today() -> date:
    from services import timeutil  # через модуль: тесты замораживают «сейчас»
    return timeutil.msk_now().date()


async def cities_with_forum_on(day: date) -> list[str]:
    codes = [c["code"] for c in await enabled_cities()] if await cities_module_on() else [default_city_code()]
    out = []
    for code in codes:
        window = await forum_window(code)
        if window and window[0] <= day <= window[1]:
            out.append(code)
    return out


def _ddmm(d: date) -> str:
    return d.strftime("%d.%m")


def _has_city(user: dict) -> bool:
    """Город делегата не записан — день не проверяем: `normalize_city` превратил бы пустоту в
    город по умолчанию (Москву), и делегат без города получал бы отказ «форум 30.10»."""
    return bool(str(user.get("event_city") or "").strip())


async def entry_day_denial(user: dict, day: date | None = None) -> dict | None:
    """`None` — сегодня (или `day`) день форума города делегата либо дата форума не задана.
    Иначе — отказ для плашки сканера: ничего не записано."""
    if not _has_city(user):
        return None
    day = day or _today()
    city = normalize_city(user.get("event_city"))
    window = await forum_window(city)
    if window is None or window[0] <= day <= window[1]:
        return None
    start = window[0]
    others = [c for c in await cities_with_forum_on(day) if c != city]
    if others:
        text = (
            f"Делегат другого города: {await city_label(city)} — у него форум {_ddmm(start)}, не сегодня. "
            "Отметка не поставлена. Проверьте, тот ли это человек; если он пришёл не на свой "
            "форум — отправьте к организаторам: перевести в другой город может менеджер."
        )
    else:
        text = (
            f"Сегодня не день форума (форум {_ddmm(start)}) — отметка не поставлена. "
            "Для пробы сканера выберите точку «🧪 Тренировка»."
        )
    return {"status": STATUS, "reason_text": text}


async def city_emphasis(day: date | None = None) -> bool:
    """Сегодня форум больше чем в одном городе — город делегата на плашке крупно."""
    return len(await cities_with_forum_on(day or _today())) > 1


async def off_day_for_scan(user: dict, scanned_at: str | None) -> bool:
    """Для CSV: скан (или загрузка, если времени в файле нет) не в день форума города
    делегата. Дата форума не задана — `False`."""
    if not _has_city(user):
        return False
    try:
        day = datetime.strptime(scanned_at[:10], "%Y-%m-%d").date() if scanned_at else _today()
    except ValueError:
        return False
    window = await forum_window(normalize_city(user.get("event_city")))
    return window is not None and not (window[0] <= day <= window[1])
