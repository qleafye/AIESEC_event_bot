"""Статистика прихода на форум — корневой aiogram-free модуль, ОДИН источник правды для бота
(`services/checkin_arrival.py`, aiosqlite) и дашборда (`dashboard/queries.py`, синхронный
sqlite3 read-only). Прецедент — `tg_media.py`/`web_theme.py`: только stdlib, в образ дашборда
копируется отдельной строкой COPY (сторож `tests/test_dashboard_docker.py`).

Здесь нет подключения к базе: модуль отдаёт SQL с параметрами (`arrival_queries`) и чистую
сборку отчёта из уже прочитанных строк (`build_report`). Каждый процесс исполняет запросы своим
драйвером — числа в боте и на дашборде считаются одними и теми же запросами.

Что считаем (таблица `checkins`, `database/db.py::init_db`):
- «Пришли» — одобренные текущего сезона с отметкой на точке «Вход» (`point = 'entry'`). Вход
  хранит ПЕРВЫЙ скан (UNIQUE(telegram_id, point)), поэтому второй день многодневного форума у
  уже пришедшего делегата на входе не пишется — «по дням» считаем людей с ЛЮБОЙ отметкой
  (вход или сессия) в этот день, отдельно — сколько из них пришли впервые.
- Отметку ставят сканер Mini App, поиск, CSV-выгрузка офлайн-сканера и `auto_session` (скан на
  сессии без входа сам ставит вход) — источник на счёт не влияет.
- `approx_time = 1` — время скана не прочиталось из CSV и подставлено время загрузки файла;
  такие отметки считаем как обычные и отдельно говорим, сколько их.
- Сессии — `program_sessions` города + число отметок `session:{id}` и заполненность зала
  (`program_halls.capacity`), если вместимость задана.
Все счётчики людей — COUNT(DISTINCT telegram_id): сегодня дублей не даёт UNIQUE(telegram_id,
point), но число не должно зависеть от того, что эту уникальность когда-нибудь ослабят.
Время в таблице отметок московское (конвенция `services.timeutil.msk_now`).
"""
from __future__ import annotations

import csv
import io

# Должна совпадать с `services.checkin.ENTRY_POINT` (сторож в tests/test_arrival_stats_260924.py).
ENTRY_POINT = "entry"
SESSION_POINT_PREFIX = "session:"

_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def approved_users_where(extra_parts: list[str], extra_params: list) -> tuple[str, list]:
    """WHERE для `users`: одобренные + городской/сезонный фрагменты вызывающего (у бота —
    `database.db._city_clause` + `event_season`, у дашборда — `_city_fragment`/`_season_sql`).
    Фрагменты используют голые имена колонок `users` (`event_city`, `season`) — запросы ниже
    кладут их только внутрь `FROM users`, без JOIN, поэтому неоднозначности нет."""
    parts = ["status = 'approved'"] + [p for p in extra_parts if p]
    return " AND ".join(parts), list(extra_params)


def arrival_queries(users_where: str, users_params: list, session_city: str | None) -> dict:
    """`{имя: (sql, params)}` — четыре запроса отчёта. `session_city=None` — сессии всех
    городов (менеджер смотрит «Все города» или модуль городов выключен)."""
    sub = f"SELECT telegram_id FROM users WHERE {users_where}"
    sessions_where = "WHERE s.city = ? " if session_city else ""
    return {
        "approved": (f"SELECT COUNT(*) FROM users WHERE {users_where}", list(users_params)),
        "arrived": (
            "SELECT COUNT(DISTINCT telegram_id), "
            "COUNT(DISTINCT CASE WHEN approx_time = 1 THEN telegram_id END) FROM checkins "
            f"WHERE point = ? AND telegram_id IN ({sub})",
            [ENTRY_POINT, *users_params],
        ),
        "days": (
            "SELECT substr(scanned_at, 1, 10) AS day, COUNT(DISTINCT telegram_id), "
            "COUNT(DISTINCT CASE WHEN point = ? THEN telegram_id END) FROM checkins "
            f"WHERE telegram_id IN ({sub}) GROUP BY day ORDER BY day",
            [ENTRY_POINT, *users_params],
        ),
        "sessions": (
            "SELECT s.id, s.city, s.day, s.start_time, s.end_time, s.title, h.name, h.capacity, "
            "COUNT(DISTINCT c.telegram_id), "
            "COUNT(DISTINCT CASE WHEN c.approx_time = 1 THEN c.telegram_id END) "
            "FROM program_sessions s "
            "LEFT JOIN program_halls h ON h.id = s.hall_id "
            f"LEFT JOIN checkins c ON c.point = '{SESSION_POINT_PREFIX}' || s.id "
            f"{sessions_where}"
            "GROUP BY s.id ORDER BY s.day, s.start_time, s.id",
            [session_city] if session_city else [],
        ),
    }


def day_short(day_iso: str) -> str:
    """`"2026-10-03"` -> `"03.10 (сб)"`; непарсящееся значение возвращается как есть."""
    try:
        y, m, d = (int(x) for x in day_iso.split("-"))
        import datetime as _dt
        wd = _WEEKDAYS[_dt.date(y, m, d).weekday()]
    except (ValueError, AttributeError):
        return day_iso or "—"
    return f"{d:02d}.{m:02d} ({wd})"


def _pct(part: int, whole: int | None) -> int | None:
    if not whole:
        return None
    return round(part * 100 / whole)


def build_report(approved, arrived_row, day_rows, session_rows) -> dict:
    """Сырые строки четырёх запросов -> отчёт. Строки принимаются кортежами/sqlite3.Row
    (индексный доступ), чтобы одинаково работать с обоими драйверами."""
    approved = int(approved or 0)
    arrived = int((arrived_row or (0, 0))[0] or 0)
    approx = int((arrived_row or (0, 0))[1] or 0)
    days = [
        {"day": r[0], "label": day_short(r[0]), "present": int(r[1] or 0), "first_entry": int(r[2] or 0)}
        for r in day_rows or []
        if r[0]
    ]
    sessions = []
    for r in session_rows or []:
        count = int(r[8] or 0)
        capacity = int(r[7]) if r[7] else None
        sessions.append({
            "id": r[0], "city": r[1], "day": r[2], "day_label": day_short(r[2]),
            "start": r[3], "end": r[4], "title": r[5] or "", "hall": r[6] or "",
            "capacity": capacity, "count": count, "approx": int(r[9] or 0),
            "fill_pct": _pct(count, capacity),
        })
    return {
        "approved": approved,
        "arrived": arrived,
        "not_arrived": max(approved - arrived, 0),
        "pct": _pct(arrived, approved),
        "approx": approx,
        "days": days,
        "sessions": sessions,
        "has_marks": arrived > 0 or any(s["count"] for s in sessions),
    }


def merge_reports(reports: list[dict]) -> dict:
    """Сумма отчётов нескольких городов — «Все города» в боте. Делегат принадлежит одному
    городу, поэтому люди по дням складываются без двойного счёта; сессии — просто вместе."""
    approved = sum(r["approved"] for r in reports)
    arrived = sum(r["arrived"] for r in reports)
    by_day: dict[str, list[int]] = {}
    for r in reports:
        for d in r["days"]:
            acc = by_day.setdefault(d["day"], [0, 0])
            acc[0] += d["present"]
            acc[1] += d["first_entry"]
    sessions = sorted(
        (s for r in reports for s in r["sessions"]),
        key=lambda s: (s["day"], s["start"], s["id"]),
    )
    return {
        "approved": approved,
        "arrived": arrived,
        "not_arrived": sum(r["not_arrived"] for r in reports),
        "pct": _pct(arrived, approved),
        "approx": sum(r["approx"] for r in reports),
        "days": [
            {"day": day, "label": day_short(day), "present": v[0], "first_entry": v[1]}
            for day, v in sorted(by_day.items())
        ],
        "sessions": sessions,
        "has_marks": any(r["has_marks"] for r in reports),
    }


def top_sessions(report: dict, limit: int) -> list[dict]:
    """Сессии по убыванию числа отметок (при равенстве — по времени) — для компактного
    сообщения в боте, где все сессии не помещаются."""
    ordered = sorted(report["sessions"], key=lambda s: (-s["count"], s["day"], s["start"], s["id"]))
    return ordered[:limit]


def csv_bytes(
    reports: list[tuple[str, dict]], city_labels: dict | None = None, total: dict | None = None,
) -> bytes:
    """CSV «Статистика прихода» (разделитель «;», UTF-8 с BOM — открывается в Excel без
    мастера импорта; тот же разделитель, что у остальных выгрузок бота). Три таблицы подряд,
    у каждой своя строка заголовков: итоги по городам, по дням, по сессиям. `reports` — пары
    (подпись города, отчёт); `city_labels` — код -> подпись для колонки «Город» у сессий;
    `total` — строка «Итого» в таблице итогов (режим «Все города»)."""
    labels = city_labels or {}
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    w.writerow(["Итоги"])
    w.writerow(["Город", "Одобрено", "Пришли", "Не пришли", "Явка, %", "Из них время примерное"])
    for label, rep in reports:
        w.writerow([label, rep["approved"], rep["arrived"], rep["not_arrived"],
                    "" if rep["pct"] is None else rep["pct"], rep["approx"]])
    if total is not None:
        w.writerow(["Итого", total["approved"], total["arrived"], total["not_arrived"],
                    "" if total["pct"] is None else total["pct"], total["approx"]])
    w.writerow([])
    w.writerow(["По дням"])
    w.writerow(["Город", "День", "На площадке (вход или сессия)", "Впервые на входе"])
    for label, rep in reports:
        for d in rep["days"]:
            w.writerow([label, d["label"], d["present"], d["first_entry"]])
    w.writerow([])
    w.writerow(["Сессии"])
    w.writerow(["Город", "День", "Время", "Зал", "Название", "Отметились", "Вместимость",
                "Заполненность, %", "Из них время примерное"])
    for _label, rep in reports:
        for s in rep["sessions"]:
            w.writerow([labels.get(s["city"], s["city"]), s["day_label"],
                        f"{s['start']}–{s['end']}", _sheet_safe(s["hall"]), _sheet_safe(s["title"]),
                        s["count"], "" if s["capacity"] is None else s["capacity"],
                        "" if s["fill_pct"] is None else s["fill_pct"], s["approx"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def _sheet_safe(value: str) -> str:
    """Название сессии вводит менеджер — ведущие `= + - @` Excel принял бы за формулу."""
    if value and value[0] in "=+-@":
        return "'" + value
    return value
