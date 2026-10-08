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


async def split_by_sender_city(admin_id: int | None, ids: list[int]) -> tuple[list[int], int]:
    """(кому уйдёт, сколько отсеяно). Город человека — `users.event_city` (без города = город
    по умолчанию, тот же `cities.city_scope`, что у фильтра «🏙 Город мероприятия»), а у
    недорегистрированных (их нет в `users`) — город из начала анкеты (`reg_started`).
    Тех, кого нет ни там ни там (чужой id в списке из файла), менеджеру города НЕ шлём: город
    у них неизвестен, а «всем подряд» — ровно та утечка в чужой город, от которой это сужение.
    Сколько отсеяно, менеджер видит на экране подтверждения."""
    code = await sender_city(admin_id)
    if code is None:
        return ids, 0
    from cities import city_scope
    from database.db import count_and_list_filtered, reg_started_only_ids_in_scope

    scope = city_scope(code)
    allowed = set(await count_and_list_filtered([{
        "field": "event_city", "value": code, "exclude": list(scope[1]) if scope else [],
    }]))
    allowed |= await reg_started_only_ids_in_scope(scope)
    kept = [tid for tid in ids if tid in allowed]
    return kept, len(ids) - len(kept)


async def restrict_to_sender_city(admin_id: int | None, ids: list[int]) -> list[int]:
    """Оставить из `ids` только людей города отправителя — см. `split_by_sender_city`."""
    kept, _dropped = await split_by_sender_city(admin_id, ids)
    return kept


async def sender_city_note(admin_id: int | None, dropped: int = 0) -> str:
    """Строка для экрана подтверждения: рассылка уйдёт только делегатам вашего города;
    `dropped` — сколько выбранных отсеяно сужением (им не уйдёт)."""
    code = await sender_city(admin_id)
    if code is None:
        return ""
    from cities import city_label
    note = f"🏙 Только делегатам вашего города: {await city_label(code)}.\n"
    if dropped:
        note += (f"⚠️ {dropped} из выбранных — из другого города или их нет в базе бота: "
                 "им не уйдёт.\n")
    return note + "\n"


# ── Сезон: рассылка по умолчанию идёт только текущему сезону ─────────────────────────────────
# Делегаты прошлого сезона остаются в базе (на проде 482 человека «YL 26/1»), и «Всем» без
# оговорок зовёт их на новое мероприятие. Менеджер видит, сколько их в выборке, и одной
# кнопкой оставляет только текущих. Пустой сезон считается текущим (`split_ids_by_season`).


async def past_season_note(ids: list[int]) -> str:
    """Строка «из них прошлого сезона: K» для экрана подсчёта; пусто, если таких нет."""
    from database.db import split_ids_by_season

    _current, past = await split_ids_by_season(ids)
    return f"\nиз них прошлого сезона: {len(past)}" if past else ""


async def current_season_only(ids: list[int]) -> list[int]:
    """Оставляет из `ids` только текущий сезон (и людей без сезона)."""
    from database.db import split_ids_by_season

    return (await split_ids_by_season(ids))[0]


async def season_default_filter() -> dict | None:
    """Условие «Текущий сезон (…)» для мастера фильтров или `None`: сезон мероприятия не задан
    либо в базе один сезон — сужать не по чему."""
    from database.db import SEASON_CURRENT, get_season_filter_options, get_setting

    event_season = (await get_setting("event_season") or "").strip()
    if not event_season or len(await get_season_filter_options()) <= 1:
        return None
    return {"field": "season", "value": SEASON_CURRENT, "label": f"Текущий сезон ({event_season})"}
