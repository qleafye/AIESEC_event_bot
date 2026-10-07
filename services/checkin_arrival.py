"""Статистика прихода для бота — исполнение общих запросов `arrival_stats.py` через aiosqlite.

SQL и сборка отчёта живут в корневом `arrival_stats.py` (его же исполняет дашборд своим
синхронным подключением), здесь — только городской/сезонный фрагмент по правилам бота
(`database.db._city_clause` + `event_season`, тот же признак текущего сезона, что у
`count_approved_current_season`) и поход в базу."""
from __future__ import annotations

import arrival_stats
from database.db import _city_clause, _connect, get_setting


async def _users_scope_parts(city_sc) -> tuple[list[str], list]:
    parts: list[str] = []
    params: list = []
    event_season = (await get_setting("event_season") or "").strip()
    if event_season:
        parts.append("(season IS NULL OR season = ?)")
        params.append(event_season)
    city_frag, city_params = _city_clause(city_sc, "event_city")
    if city_frag:
        parts.append(city_frag)
        params.extend(city_params)
    return parts, params


async def arrival_report(city_sc, session_city: str | None) -> dict:
    """`city_sc` — дескриптор `cities.city_scope(code)` (None — без фильтра по городу),
    `session_city` — код города для программы (None — сессии всех городов)."""
    parts, params = await _users_scope_parts(city_sc)
    where, where_params = arrival_stats.approved_users_where(parts, params)
    queries = arrival_stats.arrival_queries(where, where_params, session_city)
    async with _connect() as db:
        async with db.execute(*queries["approved"]) as cur:
            approved = (await cur.fetchone())[0]
        async with db.execute(*queries["arrived"]) as cur:
            arrived_row = await cur.fetchone()
        async with db.execute(*queries["days"]) as cur:
            day_rows = await cur.fetchall()
        async with db.execute(*queries["sessions"]) as cur:
            session_rows = await cur.fetchall()
    return arrival_stats.build_report(approved, arrived_row, day_rows, session_rows)


async def arrived_counts(city_sc, day: str | None = None) -> tuple[int, int]:
    """`(пришли, одобрено)` — счётчик «Пришли N из M» тем же запросом, что статистика прихода
    (одобренные текущего сезона со входом). `day` — «YYYY-MM-DD»: вход этого дня; `None` — хоть
    один вход за форум."""
    parts, params = await _users_scope_parts(city_sc)
    where, where_params = arrival_stats.approved_users_where(parts, params)
    queries = arrival_stats.arrival_queries(where, where_params, None)
    async with _connect() as db:
        async with db.execute(*queries["approved"]) as cur:
            approved = (await cur.fetchone())[0]
        async with db.execute(*arrival_stats.arrived_query(where, where_params, day)) as cur:
            arrived = (await cur.fetchone())[0]
    return int(arrived or 0), int(approved or 0)


async def floor_report(city_sc, session_city: str | None, now) -> dict:
    """«Сейчас на площадке» (бэклог №12) за день `now` (МСК, naive datetime): пришли сегодня из
    одобренных, прирост за последние 15 минут, сессии дня (идущие сейчас помечены), стойки."""
    from datetime import timedelta

    parts, params = await _users_scope_parts(city_sc)
    where, where_params = arrival_stats.approved_users_where(parts, params)
    queries = arrival_stats.floor_queries(
        where, where_params, now.strftime("%Y-%m-%d"),
        (now - timedelta(minutes=arrival_stats.RECENT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%H:%M"), session_city,
    )
    rows: dict = {}
    async with _connect() as db:
        for name in ("approved", "present", "recent"):
            async with db.execute(*queries[name]) as cur:
                rows[name] = (await cur.fetchone())[0]
        for name in ("sessions", "stands"):
            async with db.execute(*queries[name]) as cur:
                rows[name] = await cur.fetchall()
    # «Идут сейчас»: время сессий — местное время их города, поэтому флаг (последняя колонка)
    # пересчитываем по часам КАЖДОГО города строки, а не по московским из SQL.
    from services.timeutil import city_offset_hours, shift_hours
    hm_by_city: dict = {}
    fixed = []
    for row in rows["sessions"]:
        city = row[1]
        if city not in hm_by_city:
            hm_by_city[city] = shift_hours(now, await city_offset_hours(city)).strftime("%H:%M")
        hm = hm_by_city[city]
        fixed.append((*tuple(row)[:-1], 1 if row[2] <= hm < row[3] else 0))
    rows["sessions"] = fixed
    return arrival_stats.build_floor(
        rows["approved"], rows["present"], rows["recent"], rows["sessions"], rows["stands"], now=now,
    )


async def counter_day() -> str | None:
    """День, за который показывать счётчик «Пришли»: сегодня (МСК), если сегодня уже был хоть
    один вход (идёт день форума), иначе `None` — «за форум»."""
    from services.timeutil import msk_now

    today = msk_now().strftime("%Y-%m-%d")
    async with _connect() as db:
        async with db.execute(
            "SELECT EXISTS(SELECT 1 FROM checkins WHERE point = ? AND substr(scanned_at, 1, 10) = ?)",
            (arrival_stats.ENTRY_POINT, today),
        ) as cur:
            row = await cur.fetchone()
    return today if row and row[0] else None


async def count_program_sessions(city: str | None) -> int:
    """Сколько сессий в программе города — строка «Программа» экрана «🚦 Готовность к форуму».
    `city=None` — все города явно (без фильтра), а не `city = NULL`, который молча дал бы 0."""
    if city is None:
        sql, params = "SELECT COUNT(*) FROM program_sessions", ()
    else:
        sql, params = "SELECT COUNT(*) FROM program_sessions WHERE city = ?", (city,)
    async with _connect() as db:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
    return int(row[0] or 0) if row else 0


async def today_forum_codes(day: str | None) -> list[str] | None:
    """Города, где идёт форум в `day` (ISO, из `counter_day`): «Сегодня» счётчика прихода
    показывает только их — Москва с форумом через месяц не стоит в «сегодня» с нулём и не
    подмешивается в «Итого». `None`/пусто — фильтра нет (дня нет или ни у кого форума)."""
    if not day:
        return None
    from datetime import date
    from services.forum_days import forum_city_codes
    today = date.fromisoformat(day)
    return await forum_city_codes(today, today)
