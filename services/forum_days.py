"""Дни форума по городам — для фильтра рассылки «❌ Не пришли» (`checkin_entry`=`no`).

Фильтр «не пришли сегодня» без «🏙 Город мероприятия» брал одобренных ВСЕХ городов: в день
регионального форума (СПб, Тюмень) «мы тебя не видим на форуме» получила бы и Москва, у
которой форум через месяц. Теперь «не пришли» ограничено городами, у которых этот день —
день форума (своя дата города + «Сколько дней идёт»); «не пришли ни разу» — городами, чей
форум уже начался.

Без aiogram: зовётся из `database.db.count_and_list_filtered` (ленивым импортом), а её читает
и веб-процесс Mini App."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 2  # тот же дефолт, что у `sos_active_days` в реестре


async def forum_window(city: str | None) -> tuple[date, date] | None:
    """`[первый, последний]` день форума города по его СВОЕЙ дате; `None` — даты нет."""
    from domain.cities import get_setting_typed_for_city
    from services.reject_rules import forum_date_for

    raw = await forum_date_for(city)
    if not raw:
        return None
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    try:
        days = int(await get_setting_typed_for_city("sos_active_days", city))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return start, start + timedelta(days=max(days, 1) - 1)


async def forum_city_codes(day: date | None, today: date) -> list[str] | None:
    """Коды включённых городов, у которых `day` — день форума (`day=None` — форум уже начался
    к `today`). `None` — модуль городов выключен: город один, ограничивать нечего."""
    from domain.cities import cities_module_on, enabled_cities

    if not await cities_module_on():
        return None
    codes: list[str] = []
    for c in await enabled_cities():
        window = await forum_window(c["code"])
        if window is None:
            continue
        start, end = window
        if (start <= day <= end) if day is not None else (start <= today):
            codes.append(c["code"])
    return codes


async def forum_city_scopes(day_raw: str | None, today: date) -> list | None:
    """То же, что `forum_city_codes`, но дескрипторами `cities.city_scope` для SQL. `day_raw` —
    «YYYY-MM-DD» из записи фильтра (сентинел «сегодня» вызывающий уже раскрыл)."""
    from domain.cities import city_scope

    day = None
    if day_raw:
        try:
            day = datetime.strptime(day_raw, "%Y-%m-%d").date()
        except ValueError:
            return []  # кривой день — никого, а не всех
    codes = await forum_city_codes(day, today)
    if codes is None:
        return None
    return [list(city_scope(code)) for code in codes]


async def not_arrived_city_note(filters: list[dict], ids: list[int]) -> str:
    """Строка для экрана подтверждения рассылки по фильтру «❌ Не пришли»: сколько получателей
    в каждом городе и что города без форума в этот день в рассылку не попадают. Пусто — в
    фильтре нет «не пришли» или модуль городов выключен."""
    if not any(f.get("field") == "checkin_entry" and f.get("value") == "no" for f in filters):
        return ""
    from domain.cities import cities_module_on, city_label, normalize_city
    from database.db import get_user

    if not await cities_module_on():
        return ""
    counts: dict[str, int] = {}
    for tid in ids:
        user = await get_user(tid)
        code = normalize_city((user or {}).get("event_city"))
        counts[code] = counts.get(code, 0) + 1
    parts = [f"{await city_label(code)} — {n}" for code, n in sorted(counts.items())]
    by_city = "; ".join(parts) if parts else "никого"
    return (
        f"\n🏙 По городам: {by_city}.\n"
        "«Не пришли» считается только в городах, где в этот день идёт форум (по дате форума "
        "города), — остальные города в рассылку не попадают."
    )


async def day_cities_suffix(day_raw: str) -> str:
    """« — СПб, Тюмень» для подписи варианта фильтра «пришли / не пришли <день>»: чей это
    день форума. Без этого «не пришли 25.09» при форумах в разные дни не читается. `day_raw` —
    «YYYY-MM-DD» или сентинел «сегодня» (`database.db.CHECKIN_DAY_TODAY`). Модуль городов
    выключен или в этот день форума нет ни у кого — пустая строка."""
    from domain.cities import city_label
    from database.db import CHECKIN_DAY_TODAY
    from services.timeutil import msk_now

    today = msk_now().date()
    try:
        day = today if day_raw == CHECKIN_DAY_TODAY else datetime.strptime(day_raw, "%Y-%m-%d").date()
    except ValueError:
        return ""
    codes = await forum_city_codes(day, today)
    if not codes:
        return ""
    return " — " + ", ".join([await city_label(code) for code in codes])
