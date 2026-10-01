"""Рассылка менеджера, привязанного к городу, — только по его городу.

Менеджер с правом «📢 Рассылки» и привязкой к городу («👥 Роли» → город) видит в админке только
свой город, но рассылка «Все пользователи» / по фильтру без города уходила всей базе — в том
числе Москве в день регионального форума. Теперь получатели такого менеджера сужаются до его
города и для мгновенной рассылки, и для отложенной (на момент отправки, по автору строки).
Суперадмины (`config.ADMIN_IDS`) и менеджеры без привязки к городу — как раньше, без сужения.

Без aiogram: зовётся и из хендлера, и из планировщика."""
from __future__ import annotations

from config import config


async def sender_city(admin_id: int | None) -> str | None:
    """Город, которым ограничены рассылки этого человека, или `None` — без ограничения."""
    if admin_id is None or admin_id in config.ADMIN_IDS:
        return None
    from cities import cities_module_on, normalize_city
    from database.db import get_staff_city

    if not await cities_module_on():
        return None
    bound = await get_staff_city(admin_id)
    return normalize_city(bound) if bound else None


async def restrict_to_sender_city(admin_id: int | None, ids: list[int]) -> list[int]:
    """Оставить из `ids` только делегатов города отправителя (без города = город по умолчанию,
    тот же `cities.city_scope`, что у фильтра «🏙 Город мероприятия»)."""
    code = await sender_city(admin_id)
    if code is None:
        return ids
    from cities import city_scope
    from database.db import count_and_list_filtered

    scope = city_scope(code)
    allowed = set(await count_and_list_filtered([{
        "field": "event_city", "value": code, "exclude": list(scope[1]) if scope else [],
    }]))
    return [tid for tid in ids if tid in allowed]


async def sender_city_note(admin_id: int | None) -> str:
    """Строка для экрана подтверждения: рассылка уйдёт только делегатам вашего города."""
    code = await sender_city(admin_id)
    if code is None:
        return ""
    from cities import city_label
    return f"🏙 Только делегатам вашего города: {await city_label(code)}.\n\n"
