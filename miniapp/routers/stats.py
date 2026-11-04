"""Phase 19 (D-04 волна 2): «📊 Статистика геймы» для Mini App — только агрегаты.

`GET /app/api/stats/game` отдаёт результат `database.db.get_game_stats()` — той же и
ЕДИНСТВЕННОЙ агрегирующей функции, что стоит за экраном 9 бота
(`handlers/game/admin_gamification.py::show_game_stats`), поэтому числа совпадают до единицы.
RU-подписи категорий — из корневого `game_labels.category_label` (реестр), порядок —
фиксированный `GAME_CATEGORIES`, как у `render_category_bars`; нулевые категории тоже
отдаются (фронт сам решает, рисовать ли полосу).

Правило дашборда D-17 распространяется сюда (T-19-32): в ответе нет ни одного поля с
персональными данными — ни имён, ни username, ни телефонов, ни telegram_id.

Phase 40: `GET /app/api/stats/registrations` — цифры регистраций рядом, гейму не подменяя.
Те же агрегаты, что экран статистики бота (`get_stats` / города), без HTML и без людей.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from database.db import (
    GAME_CATEGORIES,
    count_past_season_users,
    get_city_counts,
    get_game_stats,
    get_returning_count,
    get_setting,
    get_stats,
)
from domain.cities import CITIES, cities_module_on, city_label, city_scope, normalize_city
from domain.game.labels import category_label

from miniapp.deps import Principal, require_cap, require_section
from miniapp.routers.checkin import _bound_city

router = APIRouter()


@router.get("/app/api/stats/game")
async def game_stats(
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("stats")),
) -> dict:
    stats = await get_game_stats()
    by_category = stats.get("by_category") or {}
    return {
        "participants": stats["participants"],
        "submissions": {
            "pending": stats["pending"],
            "approved": stats["approved"],
            "rejected": stats["rejected"],
        },
        # Сумма баллов за одобренные сдачи — та же цифра, что строка «🪙 Начислено баллов за
        # задания» на экране бота (без персональных данных, T-19-32).
        "coins_awarded": int(stats.get("coins_awarded", 0)),
        "by_category": [
            {"code": code, "label": await category_label(code), "count": int(by_category.get(code, 0))}
            for code in GAME_CATEGORIES
        ],
    }


async def _registration_city_rows(bound: str | None, season: str | None) -> list[dict]:
    """Свёртка городов как у экрана статистики бота: человеческое имя, не код.

    Привязка — одна строка. Без привязки и при включённом модуле — все города реестра;
    пустой и неизвестный код падают в город по умолчанию. Модуль выключен — разреза нет.
    """
    if bound is None and (not await cities_module_on() or not CITIES):
        return []
    rows = await get_city_counts(season=season)
    if bound is not None:
        total = pending = approved = 0
        for raw_city, cnt, pend, appr in rows:
            if normalize_city(raw_city) == bound:
                total += cnt or 0
                pending += pend or 0
                approved += appr or 0
        return [{
            "city_label": await city_label(bound),
            "total": total,
            "pending": pending,
            "approved": approved,
        }]
    per_city = {c["code"]: [0, 0, 0] for c in CITIES}
    for raw_city, cnt, pend, appr in rows:
        bucket = per_city[normalize_city(raw_city)]
        bucket[0] += cnt or 0
        bucket[1] += pend or 0
        bucket[2] += appr or 0
    return [
        {
            "city_label": await city_label(c["code"]),
            "total": per_city[c["code"]][0],
            "pending": per_city[c["code"]][1],
            "approved": per_city[c["code"]][2],
        }
        for c in CITIES
    ]


@router.get("/app/api/stats/registrations")
async def registration_stats(
    request: Request,
    p: Principal = Depends(require_cap("stats")),
    _: Principal = Depends(require_section("stats")),
) -> dict:
    """Цифры регистраций текущего сезона. Не гейма и не супердашборд.

    Город только из `_bound_city`: admin_ids не сужается, иначе привязка Principal.
    Query-параметр города не читается. Прошлые сезоны — только если число больше нуля.
    initData в лог и в JSON не кладётся.
    """
    bound = await _bound_city(request, p)
    scope = city_scope(bound) if bound is not None else None
    season = (await get_setting("event_season") or "").strip() or None
    total, top_unis = await get_stats(city_scope=scope, season=season)
    body = {
        "total": int(total or 0),
        "universities": [
            {"name": str(uni), "count": int(count)} for uni, count in top_unis
        ],
        "returning": int(await get_returning_count(city_scope=scope, season=season)),
        "city_label": await city_label(bound) if bound is not None else None,
        "cities": await _registration_city_rows(bound, season),
    }
    past = int(await count_past_season_users(season, city_scope=scope))
    if past:
        body["past_seasons"] = past
    return body
