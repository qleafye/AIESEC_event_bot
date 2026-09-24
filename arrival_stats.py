"""Статистика прихода на форум — корневой aiogram-free модуль, ОДИН источник правды для бота
(`services/checkin_arrival.py`, aiosqlite) и дашборда (`dashboard/queries.py`, синхронный
sqlite3 read-only). Прецедент — `tg_media.py`/`web_theme.py`: только stdlib, в образ дашборда
копируется отдельной строкой COPY (сторож `tests/test_dashboard_docker.py`).

Здесь нет подключения к базе: модуль отдаёт SQL с параметрами (`arrival_queries`) и чистую
сборку отчёта из уже прочитанных строк (`build_report`). Каждый процесс исполняет запросы своим
драйвером — числа в боте и на дашборде считаются одними и теми же запросами.

Что считаем (таблица `checkins`, `database/db.py::init_db`):
- «Пришли» — одобренные текущего сезона хотя бы с одним входом (`point = 'entry'`) за форум.
  Вход пишется КАЖДЫЙ день (UNIQUE(telegram_id, point, day), первый скан дня), поэтому «по
  дням» — люди со входом ЭТОГО дня, отдельно — сколько из них пришли впервые за форум (их
  самый ранний вход — в этот день). День берём как `substr(scanned_at, 1, 10)` — это и есть
  `checkins.day`, но так запросы работают и на базе, которую бот ещё не мигрировал.
- Отметку ставят сканер Mini App, поиск, CSV-выгрузка офлайн-сканера и `auto_session` (скан на
  сессии без входа сам ставит вход) — источник на счёт не влияет.
- `approx_time = 1` — время скана не прочиталось из CSV и подставлено время загрузки файла;
  такие отметки считаем как обычные и отдельно говорим, сколько их.
- Сессии — `program_sessions` города + число отметок `session:{id}` и заполненность зала
  (`program_halls.capacity`), если вместимость задана.
Все счётчики людей — COUNT(DISTINCT telegram_id): у входа строка на каждый день форума.
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
            "SELECT substr(c.scanned_at, 1, 10) AS day, COUNT(DISTINCT c.telegram_id), "
            "COUNT(DISTINCT CASE WHEN c.scanned_at = (SELECT MIN(c2.scanned_at) FROM checkins c2 "
            "WHERE c2.telegram_id = c.telegram_id AND c2.point = ?) THEN c.telegram_id END) "
            f"FROM checkins c WHERE c.point = ? AND c.telegram_id IN ({sub}) "
            "GROUP BY day ORDER BY day",
            [ENTRY_POINT, ENTRY_POINT, *users_params],
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


def arrived_query(users_where: str, users_params: list, day: str | None = None) -> tuple[str, list]:
    """Сколько одобренных (фрагмент `approved_users_where`) пришли: `day=None` — хоть один вход
    за форум (то же число, что «arrived» отчёта), «YYYY-MM-DD» — вход этого дня. Счётчик экрана
    «✅ Отметки на форуме» и сканера — тот же запрос, что статистика прихода."""
    day_sql = " AND substr(scanned_at, 1, 10) = ?" if day else ""
    return (
        "SELECT COUNT(DISTINCT telegram_id) FROM checkins "
        f"WHERE point = ?{day_sql} AND telegram_id IN (SELECT telegram_id FROM users WHERE {users_where})",
        [ENTRY_POINT, *([day] if day else []), *users_params],
    )


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
    raw = csv.writer(out, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)

    class _SafeWriter:
        # Любая строковая ячейка (подпись города, зал, название — вводит менеджер) — через
        # `_sheet_safe`, чтобы Excel не принял её за формулу; числа идут как есть.
        def writerow(self, row):
            raw.writerow([_sheet_safe(v) if isinstance(v, str) else v for v in row])

    w = _SafeWriter()
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
    w.writerow(["Город", "День", "Пришли (вход в этот день)", "Впервые на форуме"])
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
                        f"{s['start']}–{s['end']}", s["hall"], s["title"],
                        s["count"], "" if s["capacity"] is None else s["capacity"],
                        "" if s["fill_pct"] is None else s["fill_pct"], s["approx"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def _sheet_safe(value: str) -> str:
    """Строки вводит менеджер (подпись города, зал, название) — ведущие `= + - @` и
    табуляцию/перевод строки Excel принял бы за формулу (CSV injection)."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


# ── «Сейчас на площадке» (бэклог чек-ина №12): живой приход за ДЕНЬ форума ─────────────────
# Всё по одному дню (вход каждый день — D-37 в `.planning/FORUM-CHECKIN.md`), время МСК.
# Стойка = волонтёр (`checkins.by_staff_id`): считаем только живые отметки входа — сканер и
# поиск по фамилии. Файл офлайн-сканера (`csv`) и авто-вход со сканом сессии (`auto_session`)
# в стойки не идут: у первого время скана бывает примерным, второй — не работа стойки входа.
RECENT_MINUTES = 15
BUCKET_MINUTES = 15
LIVE_SOURCES = ("miniapp", "manual")

# Имя волонтёра — как в журнале площадки: снимок имени из Telegram в `venue_log`, иначе ФИО
# или @username из `users` (если волонтёр сам делегат), иначе «id N» — в `build_floor`.
_STAFF_NAME_SQL = (
    "COALESCE("
    "(SELECT v.staff_name FROM venue_log v WHERE v.staff_id = c.by_staff_id "
    "AND v.staff_name IS NOT NULL AND v.staff_name != '' ORDER BY v.id DESC LIMIT 1), "
    "(SELECT NULLIF(u.full_name, '') FROM users u WHERE u.telegram_id = c.by_staff_id), "
    "(SELECT '@' || ltrim(u.username, '@') FROM users u WHERE u.telegram_id = c.by_staff_id "
    "AND u.username IS NOT NULL AND u.username != ''))"
)


def floor_queries(
    users_where: str, users_params: list, day: str, since: str, now_hm: str,
    session_city: str | None,
) -> dict:
    """`{имя: (sql, params)}` экрана «Сейчас на площадке». `day` — «YYYY-MM-DD», `since` —
    «YYYY-MM-DD HH:MM:SS» (начало окна «за последние 15 мин»), `now_hm` — «HH:MM» (какие сессии
    идут сейчас). `session_city=None` — сессии всех городов."""
    sub = f"SELECT telegram_id FROM users WHERE {users_where}"
    city_sql = " AND s.city = ?" if session_city else ""
    city_params = [session_city] if session_city else []
    day_sql = "substr(c.scanned_at, 1, 10) = ?"
    live = ", ".join("?" for _ in LIVE_SOURCES)
    return {
        "approved": (f"SELECT COUNT(*) FROM users WHERE {users_where}", list(users_params)),
        "present": arrived_query(users_where, users_params, day),
        "recent": (
            f"SELECT COUNT(DISTINCT c.telegram_id) FROM checkins c WHERE c.point = ? AND {day_sql} "
            f"AND c.scanned_at >= ? AND c.telegram_id IN ({sub})",
            [ENTRY_POINT, day, since, *users_params],
        ),
        # Сессии этого дня: и все (срез по точкам на дашборде), и «идут сейчас» (флаг live).
        "sessions": (
            "SELECT s.id, s.city, s.start_time, s.end_time, s.title, h.name, h.capacity, "
            "COUNT(DISTINCT c.telegram_id), "
            "CASE WHEN s.start_time <= ? AND ? < s.end_time THEN 1 ELSE 0 END "
            "FROM program_sessions s LEFT JOIN program_halls h ON h.id = s.hall_id "
            f"LEFT JOIN checkins c ON c.point = '{SESSION_POINT_PREFIX}' || s.id "
            f"WHERE s.day = ?{city_sql} GROUP BY s.id ORDER BY s.start_time, s.id",
            [now_hm, now_hm, day, *city_params],
        ),
        "stands": (
            f"SELECT c.by_staff_id, {_STAFF_NAME_SQL}, c.scanned_at FROM checkins c "
            f"WHERE c.point = ? AND {day_sql} AND c.source IN ({live}) "
            f"AND c.by_staff_id IS NOT NULL AND c.telegram_id IN ({sub}) "
            "ORDER BY c.by_staff_id, c.scanned_at",
            [ENTRY_POINT, day, *LIVE_SOURCES, *users_params],
        ),
        "buckets": (
            "SELECT substr(c.scanned_at, 12, 3) || "
            f"printf('%02d', (CAST(substr(c.scanned_at, 15, 2) AS INTEGER) / {BUCKET_MINUTES}) "
            f"* {BUCKET_MINUTES}) AS slot, COUNT(DISTINCT c.telegram_id) FROM checkins c "
            f"WHERE c.point = ? AND {day_sql} AND c.telegram_id IN ({sub}) "
            "GROUP BY slot ORDER BY slot",
            [ENTRY_POINT, day, *users_params],
        ),
    }


def build_stands(stand_rows) -> list[dict]:
    """Строки `stands` (id волонтёра, имя, время скана — по возрастанию) -> стойки по убыванию
    числа сканов за день."""
    by_staff: dict = {}
    for r in stand_rows or []:
        acc = by_staff.setdefault(r[0], {"staff_id": r[0], "name": r[1] or f"id {r[0]}", "times": []})
        acc["times"].append(r[2])
    stands = [
        {"staff_id": s["staff_id"], "name": s["name"], "count": len(s["times"]),
         "first": s["times"][0], "last": s["times"][-1]}
        for s in by_staff.values()
    ]
    stands.sort(key=lambda s: (-s["count"], s["name"]))
    return stands


def build_floor(approved, present, recent, session_rows, stand_rows, bucket_rows=None) -> dict:
    """Сырые строки `floor_queries` -> отчёт «Сейчас на площадке» за день."""
    approved = int(approved or 0)
    present = int(present or 0)
    sessions = []
    for r in session_rows or []:
        count = int(r[7] or 0)
        capacity = int(r[6]) if r[6] else None
        sessions.append({
            "id": r[0], "city": r[1], "start": r[2], "end": r[3], "title": r[4] or "",
            "hall": r[5] or "", "capacity": capacity, "count": count,
            "fill_pct": _pct(count, capacity), "live": bool(r[8]),
        })
    return {
        "approved": approved,
        "present": present,
        "pct": _pct(present, approved),
        "recent": int(recent or 0),
        "sessions": sessions,
        "live_sessions": [s for s in sessions if s["live"]],
        "stands": build_stands(stand_rows),
        "buckets": [(r[0], int(r[1] or 0)) for r in bucket_rows or [] if r[0]],
    }


def merge_floors(floors: list[dict]) -> dict:
    """«Все города» в боте: сумма городских отчётов (делегат — в одном городе). Стойки здесь не
    складываются — вызывающий считает их одним запросом без городского фильтра."""
    approved = sum(f["approved"] for f in floors)
    present = sum(f["present"] for f in floors)
    sessions = sorted((s for f in floors for s in f["sessions"]), key=lambda s: (s["start"], s["id"]))
    return {
        "approved": approved, "present": present, "pct": _pct(present, approved),
        "recent": sum(f["recent"] for f in floors),
        "sessions": sessions, "live_sessions": [s for s in sessions if s["live"]],
        "stands": [], "buckets": [],
    }


def fill_buckets(sparse: list[tuple[str, int]], until_hm: str | None = None) -> list[tuple[str, int]]:
    """Плотная ось 15-минуток: от первой до последней (или до `until_hm`, если позже) — пустые
    интервалы нулями, иначе столбцы «перепрыгивают» тихие четверти часа (как
    `dashboard.queries._fill_missing_days` для дней)."""
    counts: dict[int, int] = {}
    for slot, cnt in sparse:
        try:
            hh, mm = (int(x) for x in slot.split(":"))
        except (ValueError, AttributeError):
            continue
        counts[hh * 60 + mm] = counts.get(hh * 60 + mm, 0) + cnt
    if not counts:
        return []
    first, last = min(counts), max(counts)
    if until_hm:
        try:
            hh, mm = (int(x) for x in until_hm.split(":"))
            last = max(last, (hh * 60 + mm) // BUCKET_MINUTES * BUCKET_MINUTES)
        except ValueError:
            pass
    return [
        (f"{m // 60:02d}:{m % 60:02d}", counts.get(m, 0))
        for m in range(first, last + 1, BUCKET_MINUTES)
    ]
