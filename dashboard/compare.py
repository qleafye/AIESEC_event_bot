"""Phase 26.1 Plan 01 (SD-02/SD-03/SD-04, D-16/D-17): сравнительный контекст супердашборда —
фан-аут по N базам событий, слияние в Python, TTL-кэш.

Единственное место в проекте, где собирается контекст «несколько событий рядом». НЕ зовёт
`dashboard.main.build_page_context` по событию — та функция считает разрезы, отвал, UTM и
гейму целиком (до ~60-90 SQL-запросов на базу, RESEARCH Q1), большая часть которых
сравнительным экранам не нужна; здесь — узкий набор вызовов `dashboard.queries`.

Никакого кросс-файлового связывания баз на уровне SQL, никакого запроса, видящего две базы
одновременно (CONTEXT «Архитектура: тот же код, режим «мульти»»): единственный путь к
каждой базе — своё подключение `dashboard.db.read_conn(event.db_path)` в режиме `mode=ro`,
все агрегаты считаются по каждой базе отдельно и сводятся уже на Python-стороне (сторож —
tests/test_dashboard_compare.py, ищет в этом файле SQL-ключевое слово для связывания баз).

Возвращаемый `build_compare_context(...)` словарь СЧИТАТЬ НЕИЗМЕНЯЕМЫМ — вызывающая сторона
(шаблон/маршрут плана 26.1-02) не должна его мутировать: тот же объект может быть отдан
повторно из TTL-кэша.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

import web_theme
from dashboard import queries
from dashboard.db import read_conn
from dashboard.queries import Scope

logger = logging.getLogger(__name__)

# T-26.1-01-04: страница читает N баз, `utm_table` в одиночку даёт до 61 запроса на базу —
# без кэша сравнительный экран умножал бы эту стоимость на КАЖДЫЙ заход. 60 с — тот же
# компромисс «на минуту», что и в RESEARCH/CONTEXT `<research_followups>` п.6.
CACHE_TTL_SECONDS = 60

# Ключ — (коды+пути участвующих событий, ось, выбранные сезоны); значение — (unix-время
# постройки, готовый контекст). Модульный словарь, не `functools.lru_cache` (тому нечем
# протухать по времени, только по числу вызовов, см. RESEARCH «Alternatives Considered»).
_CACHE: dict[tuple, tuple[float, dict]] = {}

# Orchestrator-ревью плана 26.1-01 (T-26.1-02, находка 1): ключ кэша собирается из СЫРЫХ
# значений `seasons` до их валидации внутри цикла по событиям (`valid_seasons` там же) —
# `?seasons=<код>:<мусор-1>`, `?seasons=<код>:<мусор-2>`, ... каждый раз даёт новый ключ и
# растит `_CACHE` без предела. Поведение кэша иначе не меняется: просто на КАЖДЫЙ промах
# сначала выкидываются протухшие по TTL записи, а если после этого записей всё ещё больше
# потолка — старейшие по времени постройки (не по времени последнего обращения: кэш не LRU).
_CACHE_MAX_ENTRIES = 64

# Каноничный порядок ступеней воронки — тот же, что в `queries.funnel()`. "Оплатили" —
# единственная ступень, которой может не быть у события (тумблер `payment_enabled`);
# остальные пять `queries.funnel()` отдаёт всегда.
_CANONICAL_FUNNEL_LABELS = (
    "Зашли", "Начали анкету", "Дошли до конца", "На модерации", "Одобрено", "Оплатили",
)

# Разрезы супердашборда — порядок фиксирован (CONTEXT: города, источники, метки кампаний).
# column="utm" не входит в ALLOWED_BREAKDOWNS queries.py — это отдельная агрегация поверх
# queries.utm_table() (поле "starts"), не queries.breakdown().
_COMPARE_CUTS: tuple[tuple[str, str], ...] = (
    ("event_city", "Города"),
    ("source", "Источники"),
    ("utm", "Метки кампаний"),
)


def reset_cache() -> None:
    """Сбрасывает TTL-кэш целиком — используется тестами и годится для будущего ручного
    сброса из админки (план не заводит для этого маршрут — при необходимости добавляется
    отдельно)."""
    _CACHE.clear()


def _prune_cache(now_ts: float) -> None:
    """Вызывается на КАЖДЫЙ промах кэша, до вставки новой записи. Сначала — все протухшие по
    TTL (дёшево и достаточно почти всегда), затем, если записей всё ещё `>= _CACHE_MAX_ENTRIES`
    — старейшие по времени постройки, пока не освободится место под новую запись."""
    expired = [key for key, (built_at, _ctx) in _CACHE.items() if now_ts - built_at >= CACHE_TTL_SECONDS]
    for key in expired:
        del _CACHE[key]
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest_first = sorted(_CACHE.items(), key=lambda item: item[1][0])
        overflow = len(_CACHE) - _CACHE_MAX_ENTRIES + 1
        for key, _ in oldest_first[:overflow]:
            del _CACHE[key]


# ── маленькие read-only помощники (дублируют `dashboard.main._read_setting` НАРОЧНО — тот
# модуль тянет FastAPI/Jinja2 на импорт, компаратору это не нужно) ──────────────────────────

def _read_setting(conn, key: str):
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _event_accent(conn) -> str:
    """Акцент серии — из пресета САМОГО события (CONTEXT: «а не случайной палитрой»),
    второй палитры для сравнения не заводится."""
    settings = {key: _read_setting(conn, key) for key in web_theme.THEME_KEYS.values()}
    return web_theme.resolve_theme(settings)["accent"]


def _describe_open_error(exc: Exception) -> str:
    """Человеческий текст для `available=False` — то, что видит менеджер на экране, а не
    трасса исключения (T-26.1-01-06: битая/удалённая база одного стека не роняет процесс)."""
    text = str(exc)
    if isinstance(exc, FileNotFoundError) or "unable to open database file" in text:
        return "файл базы не открылся"
    if "no such table" in text:
        return "в базе нет таблиц бота"
    return "файл базы не открылся"


# ── воронка одного события (та же семантика, что main._funnel_display — вынести общее
# правило нельзя без правки main.py, поэтому здесь считается самостоятельно) ────────────────

def _funnel_cells(rows: list[tuple[str, int]]) -> dict[str, dict]:
    baseline = 0
    for _, count in rows:
        if count:
            baseline = count
            break
    return {
        label: {"count": count, "pct": round(count / baseline * 100, 1) if baseline else 0}
        for label, count in rows
    }


def _funnel_missing_reason(label: str) -> str:
    if label == "Оплатили":
        return "у этого события оплата выключена"
    return "у этого события этот шаг не отслеживается"


# ── день N: сезонный старт регистрации ─────────────────────────────────────────────────────

def _days_to_event(raw_event_date: "str | None", now: datetime) -> "int | None":
    """`event_date` — свободнотекстовое поле (Pitfall 3 RESEARCH), парсим ТОЛЬКО если первые
    10 символов разбираются как `%Y-%m-%d` — иначе `None`, без исключения наружу."""
    if not raw_event_date:
        return None
    try:
        event_day = datetime.strptime(raw_event_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (event_day - now.date()).days


def _day_zero(registration_start_raw: "str | None", daily_rows: list[tuple[str, int]]) -> tuple["date | None", str]:
    """Ранняя из двух дат — старт регистрации сезона и первый день из `daily_registrations`
    — так на оси не появляется отрицательных дней, и не нужен спец-случай для базы без
    `reg_events`."""
    reg_date = None
    if registration_start_raw:
        try:
            reg_date = datetime.strptime(registration_start_raw[:10], "%Y-%m-%d").date()
        except ValueError:
            reg_date = None
    first_daily_date = None
    if daily_rows:
        try:
            first_daily_date = datetime.strptime(daily_rows[0][0], "%Y-%m-%d").date()
        except ValueError:
            first_daily_date = None
    if reg_date is not None and (first_daily_date is None or reg_date <= first_daily_date):
        return reg_date, "с начала отслеживания регистрации сезона"
    if first_daily_date is not None:
        return first_daily_date, "с первой заявки — отслеживание событий началось позже"
    return None, "нет данных для отсчёта"


# ── разрезы: чтение строк одного события под одним столбцом ───────────────────────────────

def _event_cut_rows(conn, column: str, scope: Scope) -> list[tuple[str, int]]:
    if column == "utm":
        return [
            (row["tag"], row["starts"])
            for row in queries.utm_table(conn, scope)
            if row["starts"]
        ]
    return queries.breakdown(conn, column, scope=scope, limit=10)


def _cut_reason(column: str) -> str:
    return {
        "event_city": "у этого события отключены города",
        "source": "у этого события отключён разрез по источникам",
        "utm": "у этого события отключены метки кампаний",
    }[column]


def _build_cut(column: str, title: str, raw_by_code: dict[str, dict]) -> "dict | None":
    """Разрез, погашенный тумблером у ВСЕХ событий, в список не попадает вовсе. Иначе —
    объединение значений всех НЕ погашенных событий, top-10 по суммарному количеству. Ячейка
    `{"count": n, "share": %}` (доля от общего числа заявок ЭТОГО события — без неё сравнение
    событий разного размера бессмысленно) либо `None` (погашено тумблером у этого события) +
    отдельная запись в `reasons`."""
    if not any(not raw["cut_gated"][column] for raw in raw_by_code.values()):
        return None

    totals: dict[str, int] = {}
    for raw in raw_by_code.values():
        if raw["cut_gated"][column]:
            continue
        for value, count in raw["cut_rows"].get(column, []):
            totals[value] = totals.get(value, 0) + count
    values = [value for value, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))][:10]

    cells: dict[str, "dict | None"] = {}
    reasons: dict[str, str] = {}
    for code, raw in raw_by_code.items():
        if raw["cut_gated"][column]:
            cells[code] = None
            reasons[code] = _cut_reason(column)
            continue
        rows_map = dict(raw["cut_rows"].get(column, []))
        total = raw["total"] or 0
        cells[code] = {
            value: {
                "count": rows_map[value],
                "share": round(rows_map[value] / total * 100, 1) if total else None,
            }
            for value in values
            if value in rows_map
        }
    return {"title": title, "column": column, "values": values, "cells": cells, "reasons": reasons}


# ── динамика по дням (два режима оси) ──────────────────────────────────────────────────────

def _build_dynamics_day_n(raw_by_code: dict[str, dict]) -> dict:
    per_event_series: dict[str, dict[int, int]] = {}
    max_day = 0
    for code, raw in raw_by_code.items():
        day_zero = raw["day_zero"]
        series: dict[int, int] = {}
        if day_zero is not None:
            for day_str, count in raw["daily_rows"]:
                try:
                    day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                offset = (day_date - day_zero).days
                if offset < 0:
                    continue  # T-26.1-01 day_zero — ранняя из двух дат, отрицательных быть не должно
                series[offset] = series.get(offset, 0) + count
                max_day = max(max_day, offset)
        per_event_series[code] = series
    labels = list(range(0, max_day + 1)) if raw_by_code else []
    series_out = [
        {
            "code": code,
            "name": raw["name"],
            "accent": raw["accent"],
            "values": [per_event_series[code].get(day, 0) for day in labels],
        }
        for code, raw in raw_by_code.items()
    ]
    return {"axis": "day_n", "labels": labels, "series": series_out}


def _build_dynamics_calendar(raw_by_code: dict[str, dict]) -> dict:
    all_dates: set[str] = set()
    for raw in raw_by_code.values():
        for day_str, _ in raw["daily_rows"]:
            all_dates.add(day_str)
    labels: list[str] = []
    if all_dates:
        cursor = datetime.strptime(min(all_dates), "%Y-%m-%d").date()
        end = datetime.strptime(max(all_dates), "%Y-%m-%d").date()
        while cursor <= end:
            labels.append(cursor.strftime("%Y-%m-%d"))
            cursor += timedelta(days=1)
    series_out = [
        {
            "code": code,
            "name": raw["name"],
            "accent": raw["accent"],
            "values": [dict(raw["daily_rows"]).get(day, 0) for day in labels],
        }
        for code, raw in raw_by_code.items()
    ]
    return {"axis": "calendar", "labels": labels, "series": series_out}


def _build_dynamics(axis: str, raw_by_code: dict[str, dict]) -> dict:
    if axis == "calendar":
        return _build_dynamics_calendar(raw_by_code)
    return _build_dynamics_day_n(raw_by_code)


# ── публичный контракт ──────────────────────────────────────────────────────────────────────

def build_compare_context(cfg, *, codes=None, axis="day_n", seasons=None, now=None) -> dict:
    """Собирает сравнительный контекст по `cfg.events` (при `codes` — только перечисленные
    коды, неизвестные коды молча игнорируются). `seasons` — `{код: сезон}`: переключатель
    сезона внутри события; неизвестный для события сезон молча откатывается к текущему
    (мусор в URL страницу не роняет). Города НИКОГДА не сужаются — сравниваем события,
    а не города внутри события.

    Любое исключение при открытии/чтении базы одного события (`sqlite3.Error`, `OSError`)
    перехватывается ПО СОБЫТИЮ: запись в `events` получает `available=False` и человеческий
    `error`, `logger.warning` пишется, остальные события считаются дальше (T-26.1-01-06).

    `now` — параметр только для тестов (иначе `datetime.now()`); используется и для отсечки
    TTL-кэша, и для `days_to_event`/меток свежести — так тест может управлять «течением
    времени» одной ручкой.
    """
    now = now or datetime.now()
    seasons = seasons or {}
    events = cfg.events
    if codes is not None:
        wanted = set(codes)
        events = tuple(event for event in events if event.code in wanted)

    season_key = tuple(
        sorted((event.code, seasons[event.code]) for event in events if seasons.get(event.code))
    )
    cache_key = (tuple((event.code, event.db_path) for event in events), axis, season_key)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_ctx = cached
        if now.timestamp() - cached_at < CACHE_TTL_SECONDS:
            return cached_ctx

    _prune_cache(now.timestamp())

    event_ctxs: list[dict] = []
    unavailable: list[dict] = []
    raw_by_code: dict[str, dict] = {}

    for event in events:
        try:
            with read_conn(event.db_path) as conn:
                flags = queries.dashboard_flags(conn)
                season_options = queries.season_options(conn)
                valid_seasons = {opt["value"] for opt in season_options}
                requested_season = seasons.get(event.code)
                chosen_season = requested_season if requested_season in valid_seasons else None
                scope = Scope(season=chosen_season)

                name = flags.get("event_name") or event.code
                accent = _event_accent(conn)
                kpi = queries.kpi_row(conn, scope)
                status = queries.status_totals(conn, scope)
                funnel_rows = queries.funnel(conn, scope)
                daily_rows = queries.daily_registrations(conn, scope)
                reg_start = queries.registration_start(conn, scope)
                day_zero, day_zero_note = _day_zero(reg_start, daily_rows)
                event_date_raw = _read_setting(conn, "event_date")
                days_to_event = _days_to_event(event_date_raw, now)

                cut_gated = {
                    "event_city": flags.get("event_city_enabled") != "on",
                    "source": flags.get("dashboard_block_sources") != "on",
                    "utm": flags.get("dashboard_block_utm") != "on",
                }
                cut_rows = {
                    column: _event_cut_rows(conn, column, scope)
                    for column in cut_gated
                    if not cut_gated[column]
                }
        except (sqlite3.Error, OSError) as exc:
            error = _describe_open_error(exc)
            logger.warning(
                "супердашборд: событие %r (%s) недоступно: %s", event.code, event.db_path, exc,
            )
            event_ctxs.append({
                "code": event.code, "name": event.code, "season": None, "accent": None,
                "available": False, "error": error, "kpi": None, "approved": None,
                "pending": None, "event_date": None, "days_to_event": None, "funnel": [],
                "registration_start": None, "day_zero": None, "day_zero_note": None,
                "season_options": [],
            })
            unavailable.append({"code": event.code, "name": event.code, "error": error})
            continue

        event_ctxs.append({
            "code": event.code,
            "name": name,
            "season": chosen_season or flags.get("event_season"),
            "accent": accent,
            "available": True,
            "error": None,
            "kpi": kpi,
            "approved": status["approved"],
            "pending": status["pending"],
            "event_date": event_date_raw,
            "days_to_event": days_to_event,
            "funnel": [
                {"label": label, **cell} for label, cell in _funnel_cells(funnel_rows).items()
            ],
            "registration_start": reg_start,
            "day_zero": day_zero.isoformat() if day_zero else None,
            "day_zero_note": day_zero_note,
            "season_options": season_options,
        })
        raw_by_code[event.code] = {
            "funnel_rows": funnel_rows,
            "daily_rows": daily_rows,
            "day_zero": day_zero,
            "cut_rows": cut_rows,
            "cut_gated": cut_gated,
            "total": kpi["total"],
            "accent": accent,
            "name": name,
        }

    present_labels = [
        label for label in _CANONICAL_FUNNEL_LABELS
        if any(label in dict(raw["funnel_rows"]) for raw in raw_by_code.values())
    ]
    funnel_table = []
    for label in present_labels:
        cells = {
            code: _funnel_cells(raw["funnel_rows"]).get(label) for code, raw in raw_by_code.items()
        }
        row = {"label": label, "cells": cells}
        if any(cell is None for cell in cells.values()):
            row["reason"] = _funnel_missing_reason(label)
        funnel_table.append(row)

    cuts = []
    for column, title in _COMPARE_CUTS:
        cut = _build_cut(column, title, raw_by_code)
        if cut is not None:
            cuts.append(cut)

    context = {
        "events": event_ctxs,
        "funnel_labels": present_labels,
        "funnel_table": funnel_table,
        "dynamics": _build_dynamics(axis, raw_by_code),
        "cuts": cuts,
        "unavailable": unavailable,
        "generated_at": now.isoformat(),
    }
    _CACHE[cache_key] = (now.timestamp(), context)
    return context
