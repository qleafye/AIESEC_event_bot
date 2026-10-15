"""Статистика записи на сессии — корневой модуль без aiogram, ОДИН источник правды для бота
(вкладки Google-таблицы, `services/forum/enroll_sheets.py`, aiosqlite) и дашборда
(`dashboard/queries.py`, синхронный sqlite3 read-only). Прецедент — `shared/arrival_stats.py`:
только stdlib и сосед по `shared`, подключения к базе здесь нет.

Модуль отдаёт SQL с параметрами и чистую сборку строк из уже прочитанных данных. Каждый процесс
исполняет запросы своим драйвером, поэтому числа в таблице и на дашборде считаются одинаково.

Что считаем:
- В список попадают сессии с треком (на них можно записаться). Пленарки без трека — только в
  городах, где менеджер включил «запись на общие сессии» (`common_cities`); иначе нет.
- «Записано» — число строк `session_enrollments` по сессии.
- «Пришли» — число разных telegram_id с отметкой сканера `checkins.point = 'session:{id}'`
  (все отметившиеся, не только записанные).
- «Мест» — `enroll_limit`; лимита нет — пусто, «Свободно» тоже пусто.
Время записи в базе московское (конвенция `services.infra.timeutil.msk_now`).
"""
from __future__ import annotations

from shared.arrival_stats import SESSION_POINT_PREFIX, day_short

SESSION_HEADERS = [
    "Город", "День", "Время", "Сессия", "Зал", "Трек", "Спикер",
    "Записано", "Мест", "Свободно", "Пришли",
]
PEOPLE_HEADERS = [
    "ФИО", "@ник", "telegram_id", "Город", "День", "Время", "Сессия", "Зал", "Когда записался",
]


COMMON_KEY = "session_enroll_common_optional"
COMMON_TRACK_LABEL = "Общая"
_PER_CITY_SEP = "__city__"


def common_cities(settings: dict, city_codes, *, cities_on: bool) -> list[str]:
    """Города, где включена запись на общие сессии. `settings` — {ключ: значение} из
    `bot_settings` (нужны `COMMON_KEY` и `COMMON_KEY__city__{код}`). Та же лестница, что у
    `get_setting_typed_for_city`: модуль городов выключен — читается общее значение; включён —
    непустое переопределение города, иначе общее. Нет ключа — «off»."""
    base = (settings.get(COMMON_KEY) or "off").strip()
    out = []
    for code in city_codes:
        value = base
        if cities_on:
            value = (settings.get(f"{COMMON_KEY}{_PER_CITY_SEP}{code}") or "").strip() or base
        if value == "on":
            out.append(code)
    return out


def _scope_clause(common: list[str] | tuple[str, ...]) -> tuple[str, list]:
    """Условие «сессия с треком ИЛИ общая сессия города с включённой записью»."""
    codes = [c for c in common if c]
    if not codes:
        return "s.track_id IS NOT NULL", []
    marks = ", ".join("?" for _ in codes)
    return f"(s.track_id IS NOT NULL OR s.city IN ({marks}))", codes


def sessions_query(
    city: str | None, *, with_checkins: bool = True, common: list[str] | tuple[str, ...] = (),
) -> tuple[str, list]:
    """Сессии с треком (+ общие сессии городов из `common`) + число записанных и пришедших.
    `city=None` — все города. `with_checkins=False` — для базы без таблицы `checkins`
    («Пришли» = NULL)."""
    scope_sql, scope_params = _scope_clause(common)
    arrived = (
        f"(SELECT COUNT(DISTINCT c.telegram_id) FROM checkins c "
        f"WHERE c.point = '{SESSION_POINT_PREFIX}' || s.id)"
        if with_checkins else "NULL"
    )
    sql = (
        "SELECT s.id, s.city, s.day, s.start_time, s.end_time, s.title, s.speaker, "
        "h.name AS hall_name, t.name AS track_name, s.enroll_limit, s.enroll_closed, "
        "(SELECT COUNT(*) FROM session_enrollments e WHERE e.session_id = s.id) AS enrolled, "
        f"{arrived} AS arrived "
        "FROM program_sessions s "
        "LEFT JOIN program_tracks t ON t.id = s.track_id "
        "LEFT JOIN program_halls h ON h.id = s.hall_id "
        f"WHERE {scope_sql}"
    )
    params: list = list(scope_params)
    if city is not None:
        sql += " AND s.city = ?"
        params.append(city)
    sql += " ORDER BY s.city, s.day, s.start_time, s.id"
    return sql, params


def people_query(city: str | None, *, common: list[str] | tuple[str, ...] = ()) -> tuple[str, list]:
    """Одна строка на запись: кто, на какую сессию, когда записался. Записи на общие сессии
    городов, где запись на них выключена, не показываются."""
    scope_sql, scope_params = _scope_clause(common)
    sql = (
        "SELECT e.telegram_id, u.full_name, u.username, s.city, s.day, s.start_time, "
        "s.end_time, s.title, h.name AS hall_name, e.created_at AS enrolled_at "
        "FROM session_enrollments e "
        "JOIN program_sessions s ON s.id = e.session_id "
        "LEFT JOIN users u ON u.telegram_id = e.telegram_id "
        "LEFT JOIN program_halls h ON h.id = s.hall_id "
        f"WHERE {scope_sql}"
    )
    params: list = list(scope_params)
    if city is not None:
        sql += " AND s.city = ?"
        params.append(city)
    sql += " ORDER BY s.city, s.day, s.start_time, e.created_at, e.telegram_id"
    return sql, params


def enrolled_among_query(
    users_where: str, users_params: list, session_city: str | None,
    *, common: list[str] | tuple[str, ...] = (),
) -> tuple[str, list]:
    """Сколько разных людей из выборки `users` записаны хотя бы на одну сессию с треком (или
    на общую сессию города из `common`)."""
    scope_sql, scope_params = _scope_clause(common)
    sql = (
        "SELECT COUNT(DISTINCT e.telegram_id) FROM session_enrollments e "
        "JOIN program_sessions s ON s.id = e.session_id "
        f"WHERE {scope_sql} "
        f"AND e.telegram_id IN (SELECT telegram_id FROM users WHERE {users_where})"
    )
    params: list = [*scope_params, *users_params]
    if session_city:
        sql += " AND s.city = ?"
        params.append(session_city)
    return sql, params


def _text(value) -> str:
    return "" if value is None else str(value)


def _time_range(start, end) -> str:
    start, end = _text(start), _text(end)
    if start and end:
        return f"{start}–{end}"
    return start or end


def session_summary(rows: list[dict]) -> list[dict]:
    """Добавляет к строкам `sessions_query` готовые `time` («10:00–11:00») и `free`
    (лимит − записано, не меньше нуля; нет лимита — None)."""
    out = []
    for row in rows:
        item = dict(row)
        limit = item.get("enroll_limit")
        enrolled = int(item.get("enrolled") or 0)
        item["enrolled"] = enrolled
        item["is_common"] = not item.get("track_name")
        if item["is_common"]:
            item["track_name"] = COMMON_TRACK_LABEL
        item["time"] = _time_range(item.get("start_time"), item.get("end_time"))
        item["free"] = max(int(limit) - enrolled, 0) if limit is not None else None
        out.append(item)
    return out


def stamp_short(stamp) -> str:
    """`"2026-10-14 18:54:03"` -> `"14.10 18:54"`; не разобралось — как есть, пусто — пусто."""
    raw = _text(stamp).strip()
    try:
        date_part, time_part = raw.split(" ", 1)
        _, month, day = date_part.split("-")
        return f"{int(day):02d}.{int(month):02d} {time_part[:5]}"
    except (ValueError, AttributeError):
        return raw


def _city_label(code, labels: dict[str, str]) -> str:
    code = _text(code)
    return labels.get(code, code)


def sessions_sheet_rows(rows: list[dict], labels: dict[str, str]) -> list[list]:
    """Строки вкладки «Записи на сессии» (порядок колонок — `SESSION_HEADERS`)."""
    out = []
    for r in session_summary(rows):
        out.append([
            _city_label(r.get("city"), labels),
            day_short(r["day"]) if r.get("day") else "",
            r["time"],
            _text(r.get("title")),
            _text(r.get("hall_name")),
            _text(r.get("track_name")),
            _text(r.get("speaker")),
            r["enrolled"],
            "" if r.get("enroll_limit") is None else int(r["enroll_limit"]),
            "" if r["free"] is None else r["free"],
            "" if r.get("arrived") is None else int(r["arrived"]),
        ])
    return out


def people_sheet_rows(rows: list[dict], labels: dict[str, str]) -> list[list]:
    """Строки вкладки «Кто куда записан» (порядок — `PEOPLE_HEADERS`), по ФИО без регистра,
    затем день и время сессии."""
    ordered = sorted(
        rows,
        key=lambda r: (
            _text(r.get("full_name")).casefold(), _text(r.get("day")), _text(r.get("start_time")),
        ),
    )
    out = []
    for r in ordered:
        username = _text(r.get("username")).strip().lstrip("@")
        if username == "-":  # «-» анкета пишет тем, у кого нет ника
            username = ""
        out.append([
            _text(r.get("full_name")),
            f"@{username}" if username else "",
            _text(r.get("telegram_id")),
            _city_label(r.get("city"), labels),
            day_short(r["day"]) if r.get("day") else "",
            _time_range(r.get("start_time"), r.get("end_time")),
            _text(r.get("title")),
            _text(r.get("hall_name")),
            stamp_short(r.get("enrolled_at")),
        ])
    return out
