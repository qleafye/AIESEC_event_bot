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

from database.db import settings_snapshot
from domain.cities import cities_module_on, city_label, default_city_code, enabled_cities, normalize_city

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


async def day_check_city(user: dict) -> tuple[bool, str | None]:
    """`(проверять ли день, город для окна форума)`.

    Город делегата записан — его город. Не записан при ВКЛЮЧЁННЫХ городах — день не проверяем:
    `normalize_city` превратил бы пустоту в город по умолчанию (Москву), и делегат без города
    получал бы отказ «форум 30.10». При ВЫКЛЮЧЕННЫХ городах пустой город у всех (стенд,
    конференция) — проверяем по общей `forum_date` (`forum_window(None)` её и читает), иначе
    проба сканера накануне снова ставила бы настоящую отметку."""
    raw = str(user.get("event_city") or "").strip()
    if raw:
        return True, normalize_city(raw)
    if await cities_module_on():
        return False, None
    return True, None


async def entry_day_denial(user: dict, day: date | None = None, *, bound: str | None = None) -> dict | None:
    """`None` — сегодня (или `day`) день форума города делегата либо дата форума не задана.
    Иначе — отказ для плашки сканера: ничего не записано. Настройки (даты и длительность
    форума по городам) — одним снимком `bot_settings`, а не соединением на каждый ключ: это
    путь каждого скана входа.

    `bound` — город, к которому привязан волонтёр. Совпал с городом делегата — делегат не
    «другого города», даже если сегодня форум идёт где-то ещё: у делегата просто нет форума сегодня."""
    async with settings_snapshot():
        check, city = await day_check_city(user)
        if not check:
            return None
        return await _entry_day_denial(city, day, own_city=bound is not None and bound == city)


async def _entry_day_denial(city: str | None, day: date | None, *, own_city: bool = False) -> dict | None:
    day = day or _today()
    window = await forum_window(city)
    if window is None or window[0] <= day <= window[1]:
        return None
    start = window[0]
    others = [c for c in await cities_with_forum_on(day) if c != city]
    if others and own_city:
        # Форум сегодня идёт в другом городе, но волонтёр привязан к городу делегата — делегат
        # «свой», просто сегодня у делегата нет форума.
        text = (
            f"Сегодня у делегата нет форума (форум {_ddmm(start)}) — отметка не поставлена. "
            "Для пробы сканера выберите точку «🧪 Тренировка»."
        )
    elif others:
        text = (
            f"Делегат другого города: {await city_label(city)} — у делегата форум {_ddmm(start)}, не сегодня. "
            "Отметка не поставлена. Проверьте, тот ли это человек; если форум у делегата другой "
            "— отправьте к организаторам: перевести в другой город может менеджер."
        )
    else:
        text = (
            f"Сегодня не день форума (форум {_ddmm(start)}) — отметка не поставлена. "
            "Для пробы сканера выберите точку «🧪 Тренировка»."
        )
    return {"status": STATUS, "reason_text": text}


async def city_emphasis(day: date | None = None) -> bool:
    """Сегодня форум больше чем в одном городе — город делегата на плашке крупно."""
    async with settings_snapshot():
        return len(await cities_with_forum_on(day or _today())) > 1


async def off_day_for_scan(user: dict, scanned_at: str | None) -> bool:
    """Для CSV: скан (или загрузка, если времени в файле нет) не в день форума города
    делегата. Дата форума не задана — `False`."""
    check, city = await day_check_city(user)
    if not check:
        return False
    try:
        day = datetime.strptime(scanned_at[:10], "%Y-%m-%d").date() if scanned_at else _today()
    except ValueError:
        return False
    window = await forum_window(city)
    return window is not None and not (window[0] <= day <= window[1])
