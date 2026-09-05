"""Phase 15 Plan 03 (STAT-01/STAT-04): все агрегаты дашборда — read-only, без ПД (D-17),
без кэша (D-16), считаются на лету на каждый запрос.

Каждая публичная функция — `f(conn, *, scope) -> dict|list`, где `conn` — открытое
`dashboard.db.read_conn(...)` подключение (`sqlite3.Connection`, `row_factory=sqlite3.Row`),
а `scope` — `Scope(city, season)`. Значения в SQL — ТОЛЬКО через `?`-параметры; имя колонки
для `breakdown()` — только из белого списка `ALLOWED_BREAKDOWNS` (T-15-03-02).

Этот модуль сознательно НЕ импортирует `cities.py`/`database.db`/`settings_schema.py` —
все они тянут `aiosqlite`/`aiogram`-адъянсентные модули бота, а дашборд — отдельный процесс
с отдельным (синхронным, read-only) подключением. Резолв города по умолчанию и дефолты
настроек поэтому продублированы здесь, по СЫРОЙ таблице `cities`/`bot_settings`, а не через
импорт registry-модулей бота (см. 15-CONTEXT.md `<interfaces>`). Дрейф дефолтов от
`settings_schema.SETTINGS_SCHEMA` ловит тест (`tests/test_dashboard_queries.py`), сам этот
файл `settings_schema` не импортирует.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Scope:
    """city=None -> все города; season=None -> текущий сезон (D-13)."""
    city: str | None = None
    season: str | None = None


# Дефолты продублированы из settings_schema.SETTINGS_SCHEMA — дашборд не может импортировать
# тот модуль (тянет aiosqlite через database.db). Дрейф ловит
# test_dashboard_flags_defaults_match_settings_schema.
_SETTING_DEFAULTS = {
    "dashboard_block_funnel": "on",
    "dashboard_block_dynamics": "on",
    "dashboard_block_universities": "on",
    "dashboard_block_sources": "on",
    "dashboard_block_courses": "on",
    "dashboard_block_study_fields": "on",
    "dashboard_block_dropout": "on",
    "dashboard_block_utm": "on",
    "dashboard_block_months": "on",
    "dashboard_block_game": "off",
    "payment_enabled": "off",
    "event_city_enabled": "off",
    "event_name": None,
    "event_season": None,
}


def _where(parts: list[str]) -> str:
    """`parts` — уже готовые условия (без AND/WHERE) — просто склеивает их."""
    return f" WHERE {' AND '.join(parts)}" if parts else ""


def _scalar(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


# ── bot_settings / дефолты блоков ────────────────────────────────────────────────────────

def dashboard_flags(conn) -> dict:
    """`dashboard_block_*`/`payment_enabled`/`event_city_enabled`/`event_name`/`event_season`
    из `bot_settings`, с дефолтами `_SETTING_DEFAULTS` для отсутствующих ключей."""
    keys = tuple(_SETTING_DEFAULTS)
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT key, value FROM bot_settings WHERE key IN ({placeholders})", keys
    ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return {key: values.get(key, _SETTING_DEFAULTS[key]) for key in keys}


def _current_event_season(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key = 'event_season'"
    ).fetchone()
    return row["value"] if row is not None else None


def season_options(conn) -> list[dict]:
    """Текущий сезон (из `event_season`, если задан) первым и помечен `current=True`, затем
    прочие непустые `users.season`, отсортированные по значению."""
    current = _current_event_season(conn)
    rows = conn.execute(
        "SELECT DISTINCT season FROM users "
        "WHERE season IS NOT NULL AND TRIM(season) != '' ORDER BY season"
    ).fetchall()
    seen: set[str] = set()
    options: list[dict] = []
    if current:
        options.append({"value": current, "label": current, "current": True})
        seen.add(current)
    for row in rows:
        season = row["season"]
        if season in seen:
            continue
        options.append({"value": season, "label": season, "current": False})
        seen.add(season)
    return options


def city_options(conn) -> list[dict]:
    """Включённые города из `cities`, по `sort_order` — пусто, если `event_city_enabled`
    выключен (D-19 не заводит отдельного тумблера свитчера — читаем сам master-флаг)."""
    flags = dashboard_flags(conn)
    if flags.get("event_city_enabled") != "on":
        return []
    rows = conn.execute(
        "SELECT code, label FROM cities WHERE enabled = 1 ORDER BY sort_order ASC, code ASC"
    ).fetchall()
    return [{"code": row["code"], "label": row["label"]} for row in rows]


# ── резолв города по умолчанию (повторяет cities.default_city_code/normalize_city, но по
# сырой таблице — этот модуль не импортирует cities.py, см. модульный докстринг) ──────────

def _default_city_code(conn) -> str:
    configured = os.environ.get("EVENT_CITY_DEFAULT", "msk")
    row = conn.execute("SELECT code FROM cities WHERE code = ?", (configured,)).fetchone()
    if row is not None:
        return configured
    row = conn.execute(
        "SELECT code FROM cities ORDER BY sort_order ASC, code ASC LIMIT 1"
    ).fetchone()
    if row is not None:
        return row["code"]
    return "msk"


def _known_city_codes(conn) -> list[str]:
    rows = conn.execute("SELECT code FROM cities ORDER BY sort_order ASC, code ASC").fetchall()
    return [row["code"] for row in rows]


def _city_fragment(conn, code: str) -> tuple[str, list]:
    """Параметризованный фрагмент для ОДНОГО конкретного города (не для `city=None` —
    это ветвит `_city_sql`). Город по умолчанию описан ИСКЛЮЧЕНИЕМ прочих известных кодов
    (ловит `event_city IS NULL` и любой мусорный/незнакомый код), любой другой город —
    равенством — ровно ветка `exclude` в `database.db._city_clause`."""
    default_code = _default_city_code(conn)
    if code == default_code:
        others = [c for c in _known_city_codes(conn) if c != default_code]
        if not others:
            return "event_city = ?", [code]
        placeholders = ", ".join("?" for _ in others)
        return f"(event_city IS NULL OR event_city NOT IN ({placeholders}))", others
    return "event_city = ?", [code]


def _city_sql(conn, city: str | None) -> tuple[str, list]:
    if city is None:
        return "", []
    return _city_fragment(conn, city)


def _season_sql(conn, season: str | None) -> tuple[str, list]:
    """`season=None` -> текущий сезон (D-13): `season IS NULL OR season = <event_season>`.
    Именованный сезон -> равенство."""
    if season is None:
        current = _current_event_season(conn)
        return "(season IS NULL OR season = ?)", [current]
    return "season = ?", [season]


def _scope_sql(conn, scope: Scope) -> tuple[list[str], tuple]:
    """Общий помощник для `users`/`reg_events` — обе таблицы называют колонки одинаково
    (`event_city`, `season`), поэтому один и тот же фрагмент годится для обеих."""
    city_frag, city_params = _city_sql(conn, scope.city)
    season_frag, season_params = _season_sql(conn, scope.season)
    parts = [p for p in (city_frag, season_frag) if p]
    params = tuple(city_params) + tuple(season_params)
    return parts, params


# ── KPI-строка (D-06/D-14/D-16) ──────────────────────────────────────────────────────────

def kpi_row(conn, scope: Scope) -> dict:
    parts, params = _scope_sql(conn, scope)

    total = _scalar(conn, f"SELECT COUNT(*) FROM users{_where(parts)}", params) or 0

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    prev_week_start = (now - timedelta(days=13)).strftime("%Y-%m-%d")
    prev_week_end = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    today_count = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + ['substr(registration_date, 1, 10) = ?'])}",
        params + (today,),
    ) or 0
    week_count = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + ['substr(registration_date, 1, 10) >= ?'])}",
        params + (week_start,),
    ) or 0
    prev_week_count = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users"
        f"{_where(parts + ['substr(registration_date, 1, 10) >= ?', 'substr(registration_date, 1, 10) <= ?'])}",
        params + (prev_week_start, prev_week_end),
    ) or 0

    tracking_since = _scalar(conn, f"SELECT MIN(ts) FROM reg_events{_where(parts)}", params)
    starts = _scalar(
        conn, f"SELECT COUNT(DISTINCT telegram_id) FROM reg_events{_where(parts + ['event = ?'])}",
        params + ("start",),
    ) or 0
    completed = _scalar(
        conn, f"SELECT COUNT(DISTINCT telegram_id) FROM reg_events{_where(parts + ['event = ?'])}",
        params + ("form_completed",),
    ) or 0
    conversion = round(completed / starts * 100, 1) if starts else None

    return {
        "total": total,
        "today": today_count,
        "week": week_count,
        "week_delta": week_count - prev_week_count,
        "conversion": conversion,
        "tracking_since": tracking_since,
    }


def funnel_tracking_since(conn) -> str | None:
    """С какого момента `reg_events` вообще ведутся -- НЕ со скоупом (в отличие от
    `kpi_row`'s own `tracking_since`, у которого другая семантика: подпись «Отслеживаем с»
    ДЛЯ ЭТОГО экрана/города, её мы не трогаем). `funnel()` использует это значение, чтобы
    отсечь статусные ступени («На модерации»/«Одобрено»/«Оплатили») по общему началу трекинга
    событий: если сузить отсечку скоупом города, а первое событие в этом городе случилось
    позже общего начала трекинга, статусные ступени резались бы СИЛЬНЕЕ, чем событийные
    (`start`/`form_started`/`form_completed`, которые и так живут только внутри окна
    трекинга) -- воронка снова начала бы врать, только в другую сторону: заниженные статусы
    относительно честных событий. Вопрос здесь один и тот же для всех городов: «с какого
    момента бот вообще ведёт события», а не «когда в этом городе случился первый вход»."""
    return _scalar(conn, "SELECT MIN(ts) FROM reg_events")


# ── воронка (D-07/D-08) ──────────────────────────────────────────────────────────────────

def funnel(conn, scope: Scope) -> list[tuple[str, int]]:
    flags = dashboard_flags(conn)
    parts, params = _scope_sql(conn, scope)
    tracking_since = funnel_tracking_since(conn)

    def _distinct_event_count(event: str) -> int:
        event_parts = parts + ["event = ?"]
        return _scalar(
            conn,
            f"SELECT COUNT(DISTINCT telegram_id) FROM reg_events{_where(event_parts)}",
            params + (event,),
        ) or 0

    def _status_count(status: str) -> int:
        status_parts = parts + ["status = ?"]
        status_params = params + (status,)
        # Отсечка по началу трекинга событий (см. funnel_tracking_since) -- только когда
        # reg_events не пуста. Заявки с пустым registration_date под отсечку НЕ проходят:
        # это верно, такая строка заведомо старше трекинга (иначе registration_date был бы
        # заполнен). Пустая reg_events -> веток отсечки нет вовсе, поведение прежнее.
        if tracking_since is not None:
            status_parts = status_parts + ["registration_date >= ?"]
            status_params = status_params + (tracking_since,)
        return _scalar(
            conn, f"SELECT COUNT(*) FROM users{_where(status_parts)}", status_params
        ) or 0

    stages = [
        ("Зашли", _distinct_event_count("start")),
        ("Начали анкету", _distinct_event_count("form_started")),
        ("Дошли до конца", _distinct_event_count("form_completed")),
        ("На модерации", _status_count("pending")),
        ("Одобрено", _status_count("approved")),
    ]
    if flags.get("payment_enabled") == "on":
        payment_parts = parts + ["payment_status = ?"]
        payment_params = params + ("paid",)
        if tracking_since is not None:
            payment_parts = payment_parts + ["registration_date >= ?"]
            payment_params = payment_params + (tracking_since,)
        paid = _scalar(
            conn, f"SELECT COUNT(*) FROM users{_where(payment_parts)}", payment_params
        ) or 0
        stages.append(("Оплатили", paid))
    return stages


# ── динамика по дням (D-14) ──────────────────────────────────────────────────────────────

def daily_registrations(conn, scope: Scope) -> list[tuple[str, int]]:
    """Плотный календарь: от первого дня с заявкой до последнего из (последний день с
    заявкой, сегодня) — дни без заявок отдаются нулями. Без этого линия графика
    «перепрыгивает» дыры и врёт о темпе (dataviz #7). Пусто — пустой список."""
    parts, params = _scope_sql(conn, scope)
    date_parts = parts + ["registration_date IS NOT NULL", "TRIM(registration_date) != ''"]
    rows = conn.execute(
        "SELECT substr(registration_date, 1, 10) AS day, COUNT(*) AS cnt FROM users"
        f"{_where(date_parts)} GROUP BY day ORDER BY day ASC",
        params,
    ).fetchall()
    sparse = [(row["day"], row["cnt"]) for row in rows]
    return _fill_missing_days(sparse)


def _fill_missing_days(sparse: list[tuple[str, int]]) -> list[tuple[str, int]]:
    if not sparse:
        return []
    counts: dict[str, int] = {}
    for day, cnt in sparse:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            # Битая дата в строке users — не роняем весь график, просто не ставим её на ось.
            continue
        counts[day] = counts.get(day, 0) + cnt
    if not counts:
        return sparse
    first = datetime.strptime(min(counts), "%Y-%m-%d").date()
    last = max(datetime.strptime(max(counts), "%Y-%m-%d").date(), datetime.now().date())
    result: list[tuple[str, int]] = []
    cursor = first
    while cursor <= last:
        key = cursor.strftime("%Y-%m-%d")
        result.append((key, counts.get(key, 0)))
        cursor += timedelta(days=1)
    return result


# ── «где бросают» (D-07) ─────────────────────────────────────────────────────────────────
# `reg_started` не хранит `season` (только `event_city`) — сезонное сужение здесь
# принципиально невозможно, применяется только сужение по городу.

_INCOMPLETE_NOT_REGISTERED = (
    "NOT EXISTS (SELECT 1 FROM users u WHERE u.telegram_id = reg_started.telegram_id "
    "AND (u.status IS NULL OR u.status != 'rejected'))"
)

# Дублирует handlers/reg_schema.py::REG_FLOW + REG_LABELS (step_key -> человеческий вопрос).
# Импортировать reg_schema.py нельзя — он тянет `aiogram`. Покрытие пунктов сверяет
# test_dropout_labels_cover_flow_steps на актуальном REG_FLOW бота.
_STEP_LABELS = {
    "age": "Возраст",
    "phone": "Телефон",
    "alumni_status": "Аламни/айсекер",
    "vk": "ВК",
    "city": "Город",
    "education_status": "Образование",
    "course": "Курс",
    "university": "ВУЗ",
    "study_field": "Направление обучения",
    "goal": "Цель участия",
    "formats": "Форматы форума",
    "expectations": "Ожидания (общие)",
    "source": "Источник",
    "ambassador": "Амбассадор",
    "resume": "Резюме",
    "email": "Email",
    "local_committee": "Лок. комитет",
    "position": "Позиция",
    "specialty": "Специальность",
    "work_status": "Работа",
    "work_sphere": "Сфера работы",
    "missing_skills": "Навыки",
    "attendance_format": "Формат",
    "informal_day": "Неформальный день",
    "comments": "Доп. комментарии",
    "department": "Департамент",
    "aiesec_role": "Позиция в АЙСЕК",
    "needs_certificate": "Справка в ВУЗ",
    "english_level": "Англ. язык",
    "allergies": "Аллергии",
    "food_pref": "Питание",
    "arrival": "Приезд",
    "housing": "Проживание",
    "bed_sharing": "Общая кровать",
    "bed_partner": "Сосед по кровати",
    "transport": "Трансфер",
    "cc_shop": "CC-shop",
    "exp_organizers": "Ожидания: организация",
    "exp_content": "Ожидания: контент",
    "volunteer": "Волонтёр",
    "arrival_date": "Дата приезда",
    "birth_date": "Дата рождения",
    "payment_plan_date": "Дата оплаты",
}


def _step_label(step_key: str | None) -> str:
    if not step_key:
        return "до первого вопроса"
    if step_key == "full_name":
        return "ФИО"
    if step_key.startswith("consent:"):
        return "Согласие"
    return _STEP_LABELS.get(step_key, step_key)


def dropout_steps(conn, scope: Scope) -> list[tuple[str, int]]:
    city_frag, city_params = _city_sql(conn, scope.city)
    parts = [_INCOMPLETE_NOT_REGISTERED]
    if city_frag:
        parts.append(city_frag)
    rows = conn.execute(
        f"SELECT last_step, COUNT(*) AS cnt FROM reg_started WHERE {' AND '.join(parts)} "
        "GROUP BY last_step ORDER BY cnt DESC",
        tuple(city_params),
    ).fetchall()
    return [(_step_label(row["last_step"]), row["cnt"]) for row in rows]


# ── разрезы (D-14) ───────────────────────────────────────────────────────────────────────
# Имя колонки НИКОГДА не приходит из запроса пользователя невалидированным (T-15-03-02):
# `column` попадает в f-строку SQL только после проверки `in ALLOWED_BREAKDOWNS` — тот же
# паттерн белого списка идентификаторов, что `database.db._assert_identifier` использует
# для DDL-миграций. Значения (город/сезон/лимит) — всегда `?`-параметры.
ALLOWED_BREAKDOWNS = (
    "event_city", "source", "university", "course", "study_field",
    "participant_type", "payment_option",
)


def breakdown(conn, column: str, *, scope: Scope, limit: int | None = None) -> list[tuple[str, int]]:
    if column not in ALLOWED_BREAKDOWNS:
        raise ValueError(f"Unknown breakdown column: {column!r}")

    flags = dashboard_flags(conn)
    if column == "payment_option" and flags.get("payment_enabled") != "on":
        return []
    if column == "event_city" and flags.get("event_city_enabled") != "on":
        return []

    parts, params = _scope_sql(conn, scope)
    parts = parts + [f"{column} IS NOT NULL", f"TRIM({column}) != ''", f"{column} != '-'"]
    sql = (
        f"SELECT {column} AS value, COUNT(*) AS cnt FROM users"
        f"{_where(parts)} GROUP BY {column} ORDER BY cnt DESC"
    )
    if limit:
        sql += " LIMIT ?"
        params = params + (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [(row["value"], row["cnt"]) for row in rows]


# ── сравнение городов (D-10/D-15) ────────────────────────────────────────────────────────

def city_comparison(conn, scope: Scope) -> list[dict]:
    """Строка на КАЖДЫЙ известный город — режим «все города». `scope.city` игнорируется
    (сравнение по определению не сужено на один город); `scope.season` применяется. NULL и
    незнакомые коды сворачиваются в город по умолчанию (та же логика, что и
    `render_stats_text`, но здесь — на уровне Python, не SQL, чтобы не плодить одну и ту же
    ветку исключения для каждой строки таблицы)."""
    season_frag, season_params = _season_sql(conn, scope.season)
    season_parts = [season_frag] if season_frag else []

    rows = conn.execute(
        "SELECT code, label FROM cities ORDER BY sort_order ASC, code ASC"
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        code = row["code"]
        city_frag, city_params = _city_fragment(conn, code)
        parts = season_parts + [city_frag]
        params = tuple(season_params) + tuple(city_params)
        total = _scalar(conn, f"SELECT COUNT(*) FROM users{_where(parts)}", params) or 0
        pending = _scalar(
            conn, f"SELECT COUNT(*) FROM users{_where(parts + ['status = ?'])}",
            params + ("pending",),
        ) or 0
        approved = _scalar(
            conn, f"SELECT COUNT(*) FROM users{_where(parts + ['status = ?'])}",
            params + ("approved",),
        ) or 0
        result.append({
            "code": code, "label": row["label"],
            "total": total, "pending": pending, "approved": approved,
        })
    return result


# ── метки кампаний (квик 260905-qqg, правка квик 260906-dmq) ─────────────────────────────

_UTM_LIMIT = 30

# Условие «это метка кампании, а не ручной ответ человека» (квик 260906-dmq, задача 1):
# Telegram разрешает в start-параметре deep-link только латиницу, цифры, `_` и `-`
# (https://core.telegram.org/bots/features#deep-linking) — поэтому slug-подобное значение
# `users.source` считаем меткой, а свободный текст (в т.ч. кириллический ручной ответ на
# вопрос «Источник», например «ВК») — нет. `source_from_tag = 1` (флаг живёт с 04.09:
# deep-link дошёл до конца анкеты без перезаписи ручным ответом) перекрывает эвристику там,
# где он явно проставлен — даже кириллический `source` в этом случае метка. Хвостовой `-`
# внутри класса GLOB `[^...]` — литерал, а не начало диапазона; переставлять его нельзя.
# Предикат константный, значений в него не подставляется — в f-строку попадает как есть.
_UTM_TAG_PREDICATE = [
    "source IS NOT NULL",
    "TRIM(source) != ''",
    "source != '-'",
    "(source_from_tag = 1 OR source NOT GLOB '*[^a-zA-Z0-9_-]*')",
]


def utm_table(conn, scope: Scope) -> list[dict]:
    """Мини-воронка по меткам кампаний (deep-link `/start src_<метка>`).

    Множество меток собирается из ДВУХ источников: `reg_events.source_tag` (верх воронки —
    `starts`/`form_started`) И `users.source` (низ воронки — `completed`/`approved`), а не
    только из первого. Причина: `source_tag` заполняется только с 05.09 22:57 UTC — у меток
    старше этой даты верх воронки честно нулевой (это ГРАНИЦА ТРЕКИНГА, а не баг), но заявки
    по ним уже есть в `users.source`; если бы множество меток бралось только из `source_tag`,
    такая метка выпала бы из таблицы целиком, хотя `users` ясно говорит, что заявки были.

    Заявки (`completed`/`approved`) считаются по метке ВСЕ, без отсечки по началу трекинга
    событий (`funnel_tracking_since`) — прежняя отсечка обнуляла НЕ ТОЛЬКО верх воронки (что
    честно), но и низ (что нет): строка по старой метке оставалась пустой во всех колонках
    вместо того, чтобы показать хотя бы «Заявки». `funnel()`/`kpi_row()` эту отсечку не
    теряют — она их устройства не касается, снята только здесь.

    Риск подмешивания ручного ответа на вопрос «Источник», дословно совпавшего со слагом
    кампании, сохраняется и принят (T-QQG-06, `accept`) — но `_UTM_TAG_PREDICATE` сужает его:
    попасть в `completed` теперь может только slug-подобный (латиница/цифры/`_`/`-`) ручной
    ответ, кириллический текст (например «ВК») предикат уже не пропускает.

    Строка на каждую метку из объединения обоих множеств, отсортированную по `completed` по
    убыванию, затем по `starts` по убыванию, затем по метке — не более `_UTM_LIMIT` строк.
    Отсутствующая в одном из источников метка получает нули по его колонкам (`conversion`
    считается уже после объединения, только если `starts > 0`).

    Значения — только `?`-параметры (T-QQG-01/T-DMQ-01): ни метка, ни лимит, ни город/сезон
    не попадают в f-строку; `_UTM_TAG_PREDICATE` — константные фрагменты без подстановок.
    """
    parts, params = _scope_sql(conn, scope)

    tag_parts = parts + ["source_tag IS NOT NULL", "TRIM(source_tag) != ''"]
    events_sql = (
        "SELECT source_tag AS tag, "
        "COUNT(DISTINCT CASE WHEN event = 'start' THEN telegram_id END) AS starts, "
        "COUNT(DISTINCT CASE WHEN event = 'form_started' THEN telegram_id END) AS form_started "
        "FROM reg_events"
        f"{_where(tag_parts)} GROUP BY source_tag"
    )
    events_rows = conn.execute(events_sql, params).fetchall()

    user_parts = parts + _UTM_TAG_PREDICATE
    users_sql = (
        "SELECT source AS tag, COUNT(*) AS completed, "
        "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved "
        "FROM users"
        f"{_where(user_parts)} GROUP BY source"
    )
    users_rows = conn.execute(users_sql, params).fetchall()

    events_by_tag = {row["tag"]: row for row in events_rows}
    users_by_tag = {row["tag"]: row for row in users_rows}

    result: list[dict] = []
    for tag in set(events_by_tag) | set(users_by_tag):
        event_row = events_by_tag.get(tag)
        user_row = users_by_tag.get(tag)
        starts = event_row["starts"] if event_row is not None else 0
        form_started = event_row["form_started"] if event_row is not None else 0
        completed = user_row["completed"] if user_row is not None else 0
        approved = (user_row["approved"] or 0) if user_row is not None else 0
        conversion = round(completed / starts * 100, 1) if starts else None
        result.append({
            "tag": tag,
            "starts": starts,
            "form_started": form_started,
            "completed": completed,
            "approved": approved,
            "conversion": conversion,
        })
    result.sort(key=lambda row: (-row["completed"], -row["starts"], row["tag"]))
    return result[:_UTM_LIMIT]


# ── по месяцам (квик 260906-dmq, задача 2) ────────────────────────────────────────────────

# Подписи месяцев собираются в Python из этого кортежа, а НЕ через `locale` -- он не
# гарантирован в slim-образе дашборда (модульный докстринг файла).
_MONTH_NAMES = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

_MONTHLY_LIMIT = 12
_MONTHLY_TOP_LIMIT = 3


def _month_label(ym: str) -> str:
    """`"2026-09"` -> `"Сентябрь 2026"`. Битый `ym` (не парсится в год-месяц) отдаётся как
    есть -- тот же fail-soft, что и в `_fill_missing_days`: одна кривая строка не должна
    ронять всю страницу."""
    try:
        year_str, month_str = ym.split("-")
        month_idx = int(month_str)
        if not (1 <= month_idx <= 12):
            raise ValueError(month_idx)
    except (ValueError, AttributeError):
        return ym
    return f"{_MONTH_NAMES[month_idx - 1]} {year_str}"


def monthly_table(conn, scope: Scope) -> list[dict]:
    """Заявки и одобренные помесячно + топ-3 каналов и топ-3 меток внутри месяца.

    Месяц определяется по `users.registration_date` (`substr(…, 1, 7)`), а НЕ по
    `reg_events.ts` -- иначе в одной строке смешались бы два разных определения месяца
    (заявка попадает в разбивку по дате регистрации, а не по дате первого события).
    `top_tags` поэтому тоже считается по `users.source` (тем же предикатом
    `_UTM_TAG_PREDICATE`, что и `utm_table`), а не по `reg_events.source_tag`.

    Не более `_MONTHLY_LIMIT` месяцев, свежий месяц первым (`ORDER BY ym DESC`). Заявки с
    пустым/`NULL` `registration_date` строк не создают вовсе (тот же фильтр, что у
    `daily_registrations`); заявка с непустым, но кривым `registration_date` строку создаёт --
    подпись месяца в этом случае просто не парсится и отдаётся как есть (`_month_label`),
    страница не падает. `top_sources`/`top_tags` -- списки пар (значение, число), не более
    трёх, по убыванию числа; мусорные значения (`NULL`/пустая строка/`-`) исключены тем же
    правилом, что `breakdown()`. Пусто на пустой БД -- пустой список.
    """
    parts, params = _scope_sql(conn, scope)
    date_parts = parts + ["registration_date IS NOT NULL", "TRIM(registration_date) != ''"]
    rows = conn.execute(
        "SELECT substr(registration_date, 1, 7) AS ym, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved FROM users"
        f"{_where(date_parts)} GROUP BY ym ORDER BY ym DESC LIMIT ?",
        params + (_MONTHLY_LIMIT,),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        ym = row["ym"]
        month_parts = date_parts + ["substr(registration_date, 1, 7) = ?"]
        month_params = params + (ym,)

        source_parts = month_parts + ["source IS NOT NULL", "TRIM(source) != ''", "source != '-'"]
        source_rows = conn.execute(
            f"SELECT source AS value, COUNT(*) AS cnt FROM users{_where(source_parts)} "
            "GROUP BY source ORDER BY cnt DESC, source ASC LIMIT ?",
            month_params + (_MONTHLY_TOP_LIMIT,),
        ).fetchall()

        tag_parts = month_parts + _UTM_TAG_PREDICATE
        tag_rows = conn.execute(
            f"SELECT source AS value, COUNT(*) AS cnt FROM users{_where(tag_parts)} "
            "GROUP BY source ORDER BY cnt DESC, source ASC LIMIT ?",
            month_params + (_MONTHLY_TOP_LIMIT,),
        ).fetchall()

        result.append({
            "month": _month_label(ym),
            "month_key": ym,
            "total": row["total"],
            "approved": row["approved"] or 0,
            "top_sources": [(r["value"], r["cnt"]) for r in source_rows],
            "top_tags": [(r["value"], r["cnt"]) for r in tag_rows],
        })
    return result


# ── гейма (D-12) ─────────────────────────────────────────────────────────────────────────

def game_block(conn, scope: Scope) -> dict | None:
    """`None`, если тумблер `dashboard_block_game` выключен ИЛИ в `game_submissions` нет ни
    одной строки (D-12: «включён» = тумблер + наличие данных — глобального флага модуля
    геймификации в реестре нет). Числа — по образцу `database.db.get_game_stats` (не
    переписаны «на глазок», сверены с ним тестом на одной фикстуре). Без сужения по
    городу/сезону: `game_submissions` не хранит ни то, ни другое, ровно как `get_game_stats`
    в боте."""
    flags = dashboard_flags(conn)
    if flags.get("dashboard_block_game") != "on":
        return None

    participants = _scalar(conn, "SELECT COUNT(DISTINCT user_id) FROM game_submissions") or 0
    if participants == 0:
        return None

    stats = {"participants": participants, "pending": 0, "approved": 0, "rejected": 0}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM game_submissions GROUP BY status"
    ).fetchall()
    for row in rows:
        if row["status"] in stats:
            stats[row["status"]] = row["cnt"]

    by_category: dict[str, int] = {}
    rows = conn.execute(
        "SELECT t.category AS category, COUNT(*) AS cnt FROM game_submissions s "
        "JOIN game_tasks t ON t.id = s.task_id "
        "WHERE s.status = 'approved' GROUP BY t.category"
    ).fetchall()
    for row in rows:
        by_category[row["category"]] = row["cnt"]
    stats["by_category"] = by_category
    return stats
