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

from dashboard.timeutil import msk_now


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
    "dashboard_block_referrals": "on",
    # Phase 32 (32-09, D-33/D-34): срез амбассадоров и волн — по умолчанию выключен, как и
    # dashboard_block_game, чтобы прод любого события, ещё не дошедшего до этой фазы, не
    # менялся ни на бит.
    "dashboard_block_ambassadors": "off",
    "payment_enabled": "off",
    "event_city_enabled": "off",
    "event_name": None,
    "event_season": None,
    # Phase 31 (31-03, D-15): общий рубильник модуля правил автоотказа — по умолчанию
    # выключен, ступень воронки/разбивка по правилам (31-07, D-27) гейтятся ИМЕННО этим
    # флагом, та же дисциплина, что у payment_enabled/ступени «Оплатили».
    "reject_rules_enabled": "off",
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


def _event_scope_sql(conn, scope: Scope) -> tuple[list[str], tuple]:
    """Вариант `_scope_sql` для верха воронки в `reg_events` (квик 260914-tj3): `/start`
    ещё не знает города — он выбирается позже, внутри анкеты, — поэтому у события `start`
    `event_city` всегда `NULL`. Обычный `_city_sql` в скоупе НЕ города по умолчанию
    (`event_city = ?`) отсекал бы такие строки целиком: на проде это роняло `starts` метки
    website_2 с честных 179 до 51 в скоупе конкретного города — событие есть, просто оно
    ещё не приписано ни одному городу.

    Единственное отличие от `_scope_sql` — городская часть смягчена: `event_city IS NULL`
    считается «в скоупе» для ЛЮБОГО города, не только для города по умолчанию (у которого
    `event_city IS NULL` и так входит в исключающую ветку `_city_fragment`, оборачивать
    нечего — проверка `"IS NULL" not in city_frag` это и ловит). Сезонная часть не меняется:
    сезон в deep-link уже есть на старте (`_season_sql` применяется как обычно).

    Используется ТОЛЬКО в `utm_table` для `starts`/`form_started` — не путать со
    `_scope_sql`, который используют `funnel()`/`kpi_row()`/остальные функции модуля;
    их поведение (и поведение `_scope_sql` для других вызовов внутри `utm_table`,
    т.е. подсчёт заявок по `users`) этот хелпер не меняет."""
    city_frag, city_params = _city_sql(conn, scope.city)
    if city_frag and "IS NULL" not in city_frag:
        city_frag = f"({city_frag} OR event_city IS NULL)"
    season_frag, season_params = _season_sql(conn, scope.season)
    parts = [p for p in (city_frag, season_frag) if p]
    params = tuple(city_params) + tuple(season_params)
    return parts, params


# ── KPI-строка (D-06/D-14/D-16) ──────────────────────────────────────────────────────────

def format_processing_time(minutes: float | None) -> str:
    """Человеческая подпись для «среднего времени обработки заявки».

    Подписи — для человека, не для машины: единицы сокращены (`мин`/`ч`/`д`) и склонений
    не требуют. `"—"` означает ОДНОВРЕМЕННО «решений ещё нет» (minutes is None) И «данные
    битые» (отрицательное значение) — менеджеру эти два случая различать незачем, оба
    читаются как «метрику показать нечем»."""
    if minutes is None or minutes < 0:
        return "—"
    if minutes < 1:
        return "меньше минуты"
    # Форма (минуты/часы/дни) выбирается по СЫРОМУ значению, число внутри — по округлённому
    # (`total`): иначе, например, 59.6 (< 60 сырых минут) после округления до 60 попало бы в
    # форму «1 ч» вместо ожидаемой «60 мин» — округление меняет число, но не форму подписи.
    total = round(minutes)
    if minutes < 60:
        return f"{total} мин"
    if minutes < 1440:
        hours, mins = divmod(total, 60)
        return f"{hours} ч" if mins == 0 else f"{hours} ч {mins} мин"
    days, hours = divmod(total, 1440)
    hours = hours // 60
    return f"{days} д" if hours == 0 else f"{days} д {hours} ч"


def _avg_processing_minutes(conn, parts: list[str], params: tuple) -> float | None:
    """Среднее число минут от `users.registration_date` до последнего НЕ отменённого решения
    в `application_decisions` (в скоупе `parts`/`params`, уже посчитанном `_scope_sql` —
    вызывать `_scope_sql` здесь повторно не нужно, `kpi_row` считает его один раз).

    Почему запрос устроен именно так:
    - `MAX(decided_at)` по строковым датам формата `YYYY-MM-DD HH:MM:SS` — лексикографический
      порядок совпадает с хронологическим, отдельный парсинг не нужен;
    - фильтр `undone_at IS NULL` стоит ВНУТРИ подзапроса, а не снаружи: снаружи он выбирал бы
      максимум по ВСЕМ решениям и потом отбрасывал строку целиком — делегат с отменённым
      ПОЗДНИМ решением молча выпал бы из среднего вместо того, чтобы учесть более раннее
      не-отменённое;
    - `julianday(d.decided_at) >= julianday(users.registration_date)` — повторная регистрация
      делегата (новый сезон) переписывает `users.registration_date`/`season` НА МЕСТЕ
      (`database/db.py`, `registration_date=excluded.registration_date` в UPSERT), но старые
      строки `application_decisions` остаются как были. Без этого условия делегат, ещё не
      получивший решения в НОВОМ сезоне, подхватывал бы своё решение из ПРОШЛОГО — разница
      дат уходила бы в минус и молча тянула среднее вниз. Решения раньше текущей регистрации
      исключаются целиком, а не только из финального агрегата;
    - фрагменты `parts` не квалифицированы именем таблицы — в этом JOIN `event_city`/
      `season`/`registration_date` есть только у `users`, `telegram_id` квалифицирован явно;
    - `julianday()` вернёт NULL на битой дате, `AVG` такие строки пропускает — одна кривая
      строка не роняет метрику (тот же fail-soft, что у `_month_label`);
    - `AVG` по пустому множеству даёт NULL — это и есть «решений нет».
    """
    date_parts = parts + [
        "registration_date IS NOT NULL",
        "TRIM(registration_date) != ''",
        "julianday(d.decided_at) >= julianday(users.registration_date)",
    ]
    sql = (
        "SELECT AVG((julianday(d.decided_at) - julianday(users.registration_date)) * 1440.0) "
        "FROM users JOIN (SELECT telegram_id, MAX(decided_at) AS decided_at "
        "FROM application_decisions WHERE undone_at IS NULL "
        "GROUP BY telegram_id) d ON d.telegram_id = users.telegram_id"
        f"{_where(date_parts)}"
    )
    value = _scalar(conn, sql, params)
    return round(value, 1) if value is not None else None


def _avg_game_review_minutes(conn, parts: list[str], params: tuple) -> float | None:
    """Среднее число минут от `game_submissions.submitted_at` до `reviewed_at` (решение
    менеджера по сдаче задания), в скоупе `parts`/`params`, уже посчитанном `_scope_sql`
    (вызывать `_scope_sql` здесь повторно не нужно, `kpi_row` считает его один раз).

    Почему запрос устроен именно так:
    - решение сдачи = непустой `reviewed_at`, а не `status IN ('approved', 'rejected')`:
      статус может смениться и без штампа времени в старых строках, а без штампа считать
      нечего — фильтр по `reviewed_at` и есть определение «решение принято»;
    - скоуп берётся JOIN'ом на `users`, потому что у `game_submissions` нет ни `event_city`,
      ни `season`; фрагменты `parts` не квалифицированы — эти колонки есть только у `users`,
      а `telegram_id`/`user_id` квалифицированы явно;
    - `julianday(s.reviewed_at) >= julianday(s.submitted_at)` отсекает отрицательные разницы
      (перерешённая/перенесённая строка) — иначе они молча тянули бы среднее вниз, тот же
      приём, что в `_avg_processing_minutes`;
    - `AVG` по пустому множеству даёт NULL — это и есть «решений нет».
    """
    review_parts = parts + [
        "s.reviewed_at IS NOT NULL",
        "TRIM(s.reviewed_at) != ''",
        "julianday(s.reviewed_at) >= julianday(s.submitted_at)",
    ]
    sql = (
        "SELECT AVG((julianday(s.reviewed_at) - julianday(s.submitted_at)) * 1440.0) "
        "FROM game_submissions s JOIN users ON users.telegram_id = s.user_id"
        f"{_where(review_parts)}"
    )
    value = _scalar(conn, sql, params)
    return round(value, 1) if value is not None else None


def kpi_row(conn, scope: Scope) -> dict:
    parts, params = _scope_sql(conn, scope)

    total = _scalar(conn, f"SELECT COUNT(*) FROM users{_where(parts)}", params) or 0

    now = msk_now()
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

    # Статистика «реальных заявок» — без автоотклонённых по правилам курса (Phase 31)
    no_auto_reject = "(auto_reject_rule_ids IS NULL OR TRIM(auto_reject_rule_ids) IN ('', '[]'))"
    total_real = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + [no_auto_reject])}",
        params,
    ) or 0
    today_real = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + [no_auto_reject, 'substr(registration_date, 1, 10) = ?'])}",
        params + (today,),
    ) or 0
    week_real = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + [no_auto_reject, 'substr(registration_date, 1, 10) >= ?'])}",
        params + (week_start,),
    ) or 0
    prev_week_real = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users"
        f"{_where(parts + [no_auto_reject, 'substr(registration_date, 1, 10) >= ?', 'substr(registration_date, 1, 10) <= ?'])}",
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

    processing = _avg_processing_minutes(conn, parts, params)
    # `game_review`/`question_answer` считаются ВСЕГДА (один дешёвый запрос каждый) — гашение
    # соответствующей плитки («Модерация заданий»/«Ответ на вопрос») в шаблоне по наличию
    # `game`/`questions` (не по отдельному чтению тумблера здесь): шаблон показывает плитку,
    # только когда показывается сам блок. Следующему читателю: не «чините» отсутствие проверки
    # `dashboard_block_game`/данных по вопросам в этом запросе.
    game_review = _avg_game_review_minutes(conn, parts, params)
    question_answer = _avg_question_answer_minutes(conn, parts, params)

    # Плитка «На модерации» раздела «Сейчас» (решение владельца 23.09,
    # DASHBOARD-IA-PROPOSAL-260923): сколько заявок ждёт решения менеджера прямо сейчас и
    # сколько ждёт самая старая из них. `ORDER BY julianday(...) ASC LIMIT 1`, а не `MIN`, —
    # та же причина, что у `questions_block`/`game_block`: смешанные форматы дат в
    # `registration_date` сравнивать побайтово нельзя.
    pending = _scalar(
        conn, f"SELECT COUNT(*) FROM users{_where(parts + ['status = ?'])}",
        params + ("pending",),
    ) or 0
    pending_oldest_raw = _scalar(
        conn,
        "SELECT registration_date FROM users"
        f"{_where(parts + ['status = ?'])} ORDER BY julianday(registration_date) ASC LIMIT 1",
        params + ("pending",),
    )
    pending_oldest_minutes = None
    if pending_oldest_raw:
        try:
            registered = datetime.fromisoformat(pending_oldest_raw)
        except (ValueError, TypeError):
            # Битый штамп -- не роняем страницу, просто нечем посчитать возраст (тот же
            # fail-soft, что у questions_block/game_block).
            registered = None
        if registered is not None:
            age = (msk_now() - registered).total_seconds() / 60.0
            if age >= 0:
                pending_oldest_minutes = round(age, 1)

    return {
        "total": total,
        "today": today_count,
        "week": week_count,
        "week_delta": week_count - prev_week_count,
        "total_real": total_real,
        "today_real": today_real,
        "week_real": week_real,
        "week_delta_real": week_real - prev_week_real,
        "conversion": conversion,
        "tracking_since": tracking_since,
        "processing_avg_minutes": processing,
        "processing_avg_label": format_processing_time(processing),
        "game_review_avg_minutes": game_review,
        "game_review_avg_label": format_processing_time(game_review),
        "question_answer_avg_minutes": question_answer,
        "question_answer_avg_label": format_processing_time(question_answer),
        "pending": pending,
        "pending_oldest_minutes": pending_oldest_minutes,
        "pending_oldest_label": format_processing_time(pending_oldest_minutes),
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


# ── супердашборд (Phase 26.1 Plan 01, SD-03): сезонный старт регистрации + статусы ──────────

def registration_start(conn, scope: Scope) -> str | None:
    """`MIN(ts) FROM reg_events`, суженный СЕЗОНОМ ровно так же, как `_season_sql` сужает
    всё остальное. Городская ось намеренно НЕ применяется — ось «день N» общая для события,
    а не для отдельного города (сравниваем сезоны событий, а не города внутри события).

    НЕ путать с соседним `funnel_tracking_since`: тот отвечает «с какого момента бот в этой
    базе ВООБЩЕ ведёт события» и сознательно глобален (его переиспользование сломало бы
    отсечку статусных ступеней воронки — см. его собственный докстринг). Эта функция отвечает
    на другой вопрос — «когда открылось окно регистрации ЭТОГО сезона». В базе, прожившей
    два сезона (возвратные делегаты, Phase 07.3), это разные даты: `funnel_tracking_since`
    держится за самый первый когда-либо отслеженный сезон, а `registration_start` — за
    сезон, выбранный в `scope`. Подмена одной функции другой сдвинула бы всю ось сравнения
    супердашборда на разницу между сезонами. `funnel_tracking_since` НЕ параметризуется
    скоупом умышленно — от его глобальности зависит поведение уже существующей воронки."""
    season_frag, season_params = _season_sql(conn, scope.season)
    parts = [season_frag] if season_frag else []
    return _scalar(conn, f"SELECT MIN(ts) FROM reg_events{_where(parts)}", tuple(season_params))


def status_totals(conn, scope: Scope) -> dict:
    """Итоги `users.status` по скоупу (город+сезон) — один `GROUP BY`, гарантированные ключи
    `pending`/`approved`/`rejected` (отсутствующий статус = 0, а не пропущенный ключ).

    Здесь НЕТ отсечки по `funnel_tracking_since` и не должно быть: эта функция отвечает на
    вопрос «сколько всего заявок в каждом статусе в базе», а не «сколько прошло через
    воронку отслеживаемых событий» — `funnel()` режет ступень «Одобрено» по началу трекинга,
    эта функция — нет, и числа МОГУТ законно разойтись. Это не баг и не расхождение при
    приёмке, а разная семантика двух функций."""
    parts, params = _scope_sql(conn, scope)
    totals = {"pending": 0, "approved": 0, "rejected": 0}
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS cnt FROM users{_where(parts)} GROUP BY status", params
    ).fetchall()
    for row in rows:
        if row["status"] in totals:
            totals[row["status"]] = row["cnt"]
    return totals


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
    ]
    # Phase 31 (31-07, D-27): ступень «🤖 Автоотказ» ПОСЛЕ «На модерации» и ПЕРЕД «Одобрено»
    # (воронка читается как путь заявки: сначала на модерации, дальше — либо автоотказ, либо
    # ручное решение). Гейт — `reject_rules_enabled`, та же дисциплина, что у ступени
    # «Оплатили» под `payment_enabled`: пока модуль выключен, дашборд ЛЮБОГО другого
    # мероприятия не меняется ни на бит. Тот же состав фрагментов, что у `_status_count`
    # (`_scope_sql` + собственное условие + та же отсечка `funnel_tracking_since`) — без
    # отсечки ступень считала бы импортированных делегатов вне трекинга, и воронка перестала
    # бы сходиться с соседними ступенями.
    if flags.get("reject_rules_enabled") == "on":
        auto_reject_parts = parts + [
            "(auto_reject_rule_ids IS NOT NULL AND TRIM(auto_reject_rule_ids) NOT IN ('', '[]'))"
        ]
        auto_reject_params = params
        if tracking_since is not None:
            auto_reject_parts = auto_reject_parts + ["registration_date >= ?"]
            auto_reject_params = auto_reject_params + (tracking_since,)
        auto_rejected = _scalar(
            conn, f"SELECT COUNT(*) FROM users{_where(auto_reject_parts)}", auto_reject_params
        ) or 0
        stages.append(("🤖 Автоотказ", auto_rejected))
    stages.append(("Одобрено", _status_count("approved")))
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


def _reject_rule_labels(conn) -> dict[int, str]:
    """`{id: человеческое имя}` для всех правил автоотказа — id правила менеджеру НЕ
    показываем: имя правила, если менеджер его задал, иначе первые слова текста отказа,
    иначе «Правило без названия»."""
    labels: dict[int, str] = {}
    rows = conn.execute("SELECT id, name, reject_text FROM reject_rules").fetchall()
    for row in rows:
        name = (row["name"] or "").strip()
        if name:
            labels[row["id"]] = name
            continue
        text = (row["reject_text"] or "").strip()
        if text:
            labels[row["id"]] = " ".join(text.split()[:6])
            continue
        labels[row["id"]] = "Правило без названия"
    return labels


def _live_auto_reject_rows(conn, scope: Scope):
    """Строки `auto_reject_log`, которые считаются ЖИВЫМИ для отчётности (D-H, квик 260923):
    не возвращены на модерацию И делегат всё ещё `status='rejected'` — сам поправивший анкету
    делегат (статус успел смениться, а строка журнала ещё не отмечена возвратом) больше не
    считается. Общий шов для `auto_reject_breakdown`/`auto_reject_people_count` — счётчик и
    разбивка обязаны ходить по ОДНОМУ набору условий, иначе «сумма правил» разойдётся с «числом
    людей» не по смыслу (несколько правил на человека), а по рассинхрону выборок."""
    parts, params = _scope_sql(conn, scope)
    where_parts = parts + ["l.returned_to_moderation_at IS NULL", "u.status = 'rejected'"]
    where = _where(where_parts)
    return conn.execute(
        "SELECT l.telegram_id, l.rule_ids FROM auto_reject_log l "
        f"JOIN users u ON u.telegram_id = l.telegram_id{where}",
        params,
    ).fetchall()


def auto_reject_people_count(conn, scope: Scope) -> int:
    """D-H: число РАЗНЫХ людей за живыми строками (см. `_live_auto_reject_rows`) — один человек
    может попасть под несколько правил разом, поэтому сумма `auto_reject_breakdown` может быть
    БОЛЬШЕ этого числа; дашборд подписывает разницу отдельной строкой, когда это так."""
    rows = _live_auto_reject_rows(conn, scope)
    return len({row["telegram_id"] for row in rows})


def auto_reject_breakdown(conn, scope: Scope) -> list[tuple[str, int]]:
    """Разбивка «какое правило сколько отсеяло» — по ЖИВЫМ (не возвращённым на модерацию,
    делегат всё ещё `status='rejected'` — D-H) строкам `auto_reject_log`, под тем же
    `_scope_sql`, что и `funnel()`. `rule_ids` — это JSON-список id правил ОДНОЙ колонкой:
    SQLite не умеет группировать список внутри ячейки, а объём журнала измеряется сотнями
    строк — Counter в Python дешевле второй таблицы связей. Битая строка JSON пропускается с
    продолжением: одна кривая запись не имеет права уронить дашборд. Отсортировано по убыванию —
    самое широкое (проблемное) правило видно первым."""
    import json
    from collections import Counter

    rows = _live_auto_reject_rows(conn, scope)
    labels = _reject_rule_labels(conn)
    counter: Counter[str] = Counter()
    for row in rows:
        try:
            rule_ids = json.loads(row["rule_ids"])
        except (TypeError, ValueError):
            continue
        if not isinstance(rule_ids, list):
            continue
        for rule_id in rule_ids:
            try:
                rule_id = int(rule_id)
            except (TypeError, ValueError):
                continue
            counter[labels.get(rule_id, "Правило без названия")] += 1
    return counter.most_common()


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
    last = max(datetime.strptime(max(counts), "%Y-%m-%d").date(), msk_now().date())
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

    Верх воронки (`starts`/`form_started`) считается через `_event_scope_sql`, а не
    `_scope_sql` (квик 260914-tj3): `/start` ещё не знает города (тот выбирается позже, в
    анкете) — обычный городской скоуп отсекал бы почти все старты. Подробности — в докстринге
    `_event_scope_sql`. Заявки (`completed`/`approved`) по-прежнему в обычном `_scope_sql`:
    у `users` город к моменту завершения анкеты уже известен.

    Заявки считаются ДВАЖДЫ: `completed`/`approved` — все, без отсечки по началу трекинга
    событий (`funnel_tracking_since`), и `completed_tracked`/`approved_tracked` — только те,
    чей `registration_date` не раньше этой отсечки. Обе пары нужны по разным причинам:
    - `completed`/`approved` без отсечки — прежняя отсечка обнуляла НЕ ТОЛЬКО верх воронки
      (что честно), но и низ (что нет): строка по старой метке оставалась пустой во всех
      колонках вместо того, чтобы показать хотя бы «Заявки». `funnel()`/`kpi_row()` эту
      отсечку не теряют — она их устройства не касается, снята только здесь;
    - `completed_tracked` — потому что `conversion` делится на `starts`, а `starts` живёт
      ТОЛЬКО внутри окна трекинга событий (без событий делить не на что). Деление
      all-time `completed` на `starts`, урезанный окном трекинга, давало на проде >100%
      (website_2: 114 заявок за всё время / 51 старт внутри окна = 223.5%) — у метки,
      прожившей на проде дольше трекинга событий, часть заявок физически не могла оставить
      след в `reg_events`. `conversion` теперь делит tracked-заявки на tracked-старты —
      те же ворота, что и в `funnel()`. Когда `funnel_tracking_since` пуст (в базе ещё нет
      ни одного `reg_events`), `completed_tracked`/`approved_tracked` равны `completed`/
      `approved` — отсекать не от чего.

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
    event_parts, event_params = _event_scope_sql(conn, scope)
    parts, params = _scope_sql(conn, scope)
    tracking_since = funnel_tracking_since(conn)

    tag_parts = event_parts + ["source_tag IS NOT NULL", "TRIM(source_tag) != ''"]
    events_sql = (
        "SELECT source_tag AS tag, "
        "COUNT(DISTINCT CASE WHEN event = 'start' THEN telegram_id END) AS starts, "
        "COUNT(DISTINCT CASE WHEN event = 'form_started' THEN telegram_id END) AS form_started "
        "FROM reg_events"
        f"{_where(tag_parts)} GROUP BY source_tag"
    )
    events_rows = conn.execute(events_sql, event_params).fetchall()

    user_parts = parts + _UTM_TAG_PREDICATE
    if tracking_since is not None:
        users_sql = (
            "SELECT source AS tag, COUNT(*) AS completed, "
            "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN registration_date >= ? THEN 1 ELSE 0 END) AS completed_tracked, "
            "SUM(CASE WHEN registration_date >= ? AND status = 'approved' THEN 1 ELSE 0 END) "
            "AS approved_tracked "
            "FROM users"
            f"{_where(user_parts)} GROUP BY source"
        )
        users_rows = conn.execute(
            users_sql, (tracking_since, tracking_since) + params
        ).fetchall()
    else:
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
        if tracking_since is not None:
            completed_tracked = (user_row["completed_tracked"] or 0) if user_row is not None else 0
            approved_tracked = (user_row["approved_tracked"] or 0) if user_row is not None else 0
        else:
            completed_tracked = completed
            approved_tracked = approved
        conversion = round(completed_tracked / starts * 100, 1) if starts else None
        result.append({
            "tag": tag,
            "starts": starts,
            "form_started": form_started,
            "completed": completed,
            "approved": approved,
            "completed_tracked": completed_tracked,
            "approved_tracked": approved_tracked,
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

_GAME_TOP_TASKS_LIMIT = 5


def _task_title(title, text) -> str:
    """Приватная копия правила `database.db.task_title` — тем модулем и владеет (импортировать
    нельзя, он тянет aiosqlite, см. модульный докстринг файла). Дрейф между двумя копиями
    ловит `test_task_title_matches_bot_db_task_title`, тот же приём, что у `_SETTING_DEFAULTS`
    / `_STEP_LABELS`."""
    title = str(title or "").strip()
    if title:
        return title
    text = str(text or "")
    first_line = text.splitlines()[0] if text else ""
    if len(first_line) > 40:
        return first_line[:40] + "…"
    return first_line


def _user_scoped_parts(parts: list[str]) -> list[str]:
    """`_scope_sql` фрагменты не квалифицированы (`event_city`/`season`) — этого достаточно,
    когда `users` — единственная таблица в запросе с такими колонками. Но `game_tasks` ТОЖЕ
    хранит `event_city` (задания по городу) — любой запрос, джойнящий и `users`, и
    `game_tasks`, должен квалифицировать имя, иначе SQLite падает на «ambiguous column
    name». Фрагменты `_city_fragment`/`_season_sql` содержат `event_city`/`season` только как
    имена колонок (не как часть другого идентификатора или строки), поэтому текстовая замена
    безопасна."""
    return [p.replace("event_city", "users.event_city").replace("season", "users.season") for p in parts]


def game_block(conn, scope: Scope) -> dict | None:
    """`None`, если тумблер `dashboard_block_game` выключен ИЛИ в скоупе страницы (город +
    сезон) нет ни одной сдачи (D-12: «включён» = тумблер + наличие данных В СКОУПЕ).

    ВАЖНОЕ ИЗМЕНЕНИЕ СЕМАНТИКИ (квик 260910-qgn): раньше блок считался ПО ВСЕЙ базе
    (зеркалило `database.db.get_game_stats`), теперь — по скоупу страницы, через
    `JOIN users ON users.telegram_id = s.user_id` + `parts` из `_scope_sql`. Причина: при
    включённом модуле городов менеджер, привязанный к своему городу, видел в этом блоке чужие
    города — остальной дашборд так не делает («чужой город не виден вообще»), и по
    городу/сезону блок расходился со всеми соседними числами на той же странице. Каждый
    играющий — одобренный делегат, у него всегда есть строка в `users`, поэтому JOIN никого
    не теряет; сезон `NULL` попадает в текущий сезон обычной веткой `_season_sql`.
    """
    flags = dashboard_flags(conn)
    if flags.get("dashboard_block_game") != "on":
        return None

    parts, params = _scope_sql(conn, scope)

    participants_sql = (
        "SELECT COUNT(DISTINCT s.user_id) FROM game_submissions s "
        "JOIN users ON users.telegram_id = s.user_id"
        f"{_where(parts)}"
    )
    participants = _scalar(conn, participants_sql, params) or 0
    if participants == 0:
        return None

    stats = {"participants": participants, "pending": 0, "approved": 0, "rejected": 0}
    status_sql = (
        "SELECT s.status AS status, COUNT(*) AS cnt FROM game_submissions s "
        "JOIN users ON users.telegram_id = s.user_id"
        f"{_where(parts)} GROUP BY s.status"
    )
    for row in conn.execute(status_sql, params).fetchall():
        if row["status"] in stats:
            stats[row["status"]] = row["cnt"]
    submissions_total = stats["pending"] + stats["approved"] + stats["rejected"]
    stats["submissions_total"] = submissions_total
    stats["approved_share"] = (
        round(stats["approved"] / submissions_total * 100, 1) if submissions_total else None
    )

    approved_delegates = _scalar(
        conn, f"SELECT COUNT(*) FROM users{_where(parts + ['status = ?'])}",
        params + ("approved",),
    ) or 0
    stats["participants_share"] = (
        round(participants / approved_delegates * 100, 1) if approved_delegates else None
    )

    # `c.delta > 0` — вопрос менеджера «сколько НАЧИСЛЕНО», а не «каков баланс»: журнал
    # append-only, списания в магазине не должны уменьшать начисленное. `coins_legacy`
    # отдельным ключом НЕ заводится — `coins_total` не равен сумме `coins_task + coins_manual`
    # ровно на легаси-строки с `source IS NULL` (записи из прошлого/системные) — это ожидаемое
    # расхождение, а не баг.
    coins_sql = (
        "SELECT SUM(c.delta), "
        "SUM(CASE WHEN c.source = 'task' THEN c.delta ELSE 0 END), "
        "SUM(CASE WHEN c.source = 'manual' THEN c.delta ELSE 0 END) "
        "FROM coins c JOIN users ON users.telegram_id = c.user_id"
        f"{_where(parts + ['c.delta > 0'])}"
    )
    coins_row = conn.execute(coins_sql, params).fetchone()
    coins_total = coins_row[0] or 0
    stats["coins_total"] = coins_total
    stats["coins_task"] = coins_row[1] or 0
    stats["coins_manual"] = coins_row[2] or 0
    stats["coins_per_participant"] = round(coins_total / participants, 1)

    top_sql = (
        "SELECT t.id AS id, t.title AS title, t.text AS text, COUNT(*) AS submissions, "
        "SUM(CASE WHEN s.status = 'approved' THEN 1 ELSE 0 END) AS approved "
        "FROM game_submissions s JOIN users ON users.telegram_id = s.user_id "
        "JOIN game_tasks t ON t.id = s.task_id"
        f"{_where(_user_scoped_parts(parts))} GROUP BY t.id ORDER BY submissions DESC, t.id ASC LIMIT ?"
    )
    top_rows = conn.execute(top_sql, params + (_GAME_TOP_TASKS_LIMIT,)).fetchall()
    stats["top_tasks"] = [
        {
            "title": _task_title(row["title"], row["text"]),
            "submissions": row["submissions"],
            "approved": row["approved"],
        }
        for row in top_rows
    ]

    pending_oldest_raw = _scalar(
        conn,
        "SELECT MIN(s.submitted_at) FROM game_submissions s "
        "JOIN users ON users.telegram_id = s.user_id"
        f"{_where(parts + ['s.status = ?'])}",
        params + ("pending",),
    )
    pending_oldest_minutes = None
    if pending_oldest_raw:
        try:
            submitted = datetime.strptime(pending_oldest_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Битая дата в submitted_at -- не роняем страницу, просто нечем посчитать
            # возраст (тот же fail-soft, что _month_label/_fill_missing_days).
            submitted = None
        if submitted is not None:
            pending_oldest_minutes = round((msk_now() - submitted).total_seconds() / 60.0, 1)
    stats["pending_oldest_minutes"] = pending_oldest_minutes
    stats["pending_oldest_label"] = format_processing_time(pending_oldest_minutes)

    by_category: dict[str, int] = {}
    category_sql = (
        "SELECT t.category AS category, COUNT(*) AS cnt FROM game_submissions s "
        "JOIN users ON users.telegram_id = s.user_id "
        "JOIN game_tasks t ON t.id = s.task_id"
        f"{_where(_user_scoped_parts(parts) + ['s.status = ?'])} GROUP BY t.category"
    )
    for row in conn.execute(category_sql, params + ("approved",)).fetchall():
        by_category[row["category"]] = row["cnt"]
    stats["by_category"] = by_category

    return stats


# ── вопросы делегатов (квик 260910-tt5) ──────────────────────────────────────────────────

_QUESTIONS_TOP_MANAGERS_LIMIT = 3

# Зеркало `services/questions.py::question_status` — ТРИ состояния, порядок веток ФИКСИРОВАН
# (сначала `delivered_at`, потом `answered_by`): легаси-строка, которой каким-то образом
# проставили доставку без захвата, обязана читаться как «отвечен», а не «в работе». Копия, а
# не импорт — по D-3 (`dashboard/Dockerfile` копирует только `dashboard/`, `web_theme.py`,
# `tg_media.py`; импорт `services.questions` дал бы `ModuleNotFoundError` на старте
# контейнера, тот же класс аварии, что был с `tg_media` 10.09). Паритет с оригиналом закрыт
# `test_question_status_case_matches_services_question_status`. В отличие от трёх независимых
# предикатов (см. `database.db._QUESTION_STATUS_SQL`), ветки CASE взаимоисключающие — три
# счётчика из `questions_block` гарантированно дают в сумме `total`.
_QUESTION_STATUS_CASE = (
    "CASE WHEN q.delivered_at IS NOT NULL THEN 'answered' "
    "WHEN q.answered_by IS NOT NULL THEN 'in_work' "
    "ELSE 'new' END"
)


def _avg_question_answer_minutes(conn, parts: list[str], params: tuple) -> float | None:
    """Среднее число минут от `delegate_questions.asked_at` (вопрос задан) до `delivered_at`
    (ответ ДОШЁЛ до делегата), в скоупе `parts`/`params`, уже посчитанном `_scope_sql`.

    D-1: считаем ИМЕННО `delivered_at`, а не `answered_at`. `answered_at` в этой схеме ставит
    `claim_question` — момент, когда менеджер ВЗЯЛ вопрос, а не когда на него ОТВЕТИЛ. Плитка
    «Ответ на вопрос», посчитанная по `answered_at`, показывала бы время РЕАКЦИИ и
    систематически занижала бы реальное ожидание делегата (вопрос, взятый за минуту и
    доставленный только через сутки, дал бы «1 мин»). Единственный штамп «делегат получил
    ответ» — `delivered_at` (`set_question_answer` ставит его ТОЛЬКО после успешной отправки).

    Скоуп берётся JOIN'ом на `users`, потому что у `delegate_questions` нет ни `event_city`,
    ни `season`; фрагменты `parts` не квалифицированы именем таблицы — этих колонок нет у
    второй таблицы в запросе, поэтому `_user_scoped_parts` здесь не нужен (в отличие от
    `game_block`, где `game_tasks` тоже хранит `event_city`).

    `julianday()` разбирает ОБА формата, что реально пишутся в БД: ISO с «T» и микросекундами
    (`datetime.utcnow().isoformat()`, текущий формат `create_question`/`set_question_answer`)
    и легаси-строку с пробелом (`%Y-%m-%d %H:%M:%S`) — обе лексикографически сортируемые формы
    SQLite понимает без предварительного парсинга. `julianday(q.delivered_at) >=
    julianday(q.asked_at)` отсекает отрицательные разницы (битая/перепутанная строка) — тот же
    приём, что в `_avg_processing_minutes`/`_avg_game_review_minutes`; `AVG` по пустому
    множеству даёт NULL — это и есть «отвеченных вопросов нет».
    """
    answer_parts = parts + [
        "q.delivered_at IS NOT NULL",
        "TRIM(q.delivered_at) != ''",
        "julianday(q.delivered_at) >= julianday(q.asked_at)",
    ]
    sql = (
        "SELECT AVG((julianday(q.delivered_at) - julianday(q.asked_at)) * 1440.0) "
        "FROM delegate_questions q JOIN users ON users.telegram_id = q.user_id"
        f"{_where(answer_parts)}"
    )
    value = _scalar(conn, sql, params)
    return round(value, 1) if value is not None else None


def questions_block(conn, scope: Scope) -> dict | None:
    """`None`, если в скоупе страницы (город + сезон) нет ни одного вопроса делегата.

    D-2: тумблера `dashboard_block_questions` НЕТ и заводить его не нужно — в отличие от
    геймы (отключаемый модуль, дефолт «off», может быть выключен при живых данных), вопрос
    задать может любой делегат, это базовая функция бота, а не модуль. Единственный
    осмысленный гейт — наличие данных в скоупе, ровно как у `city_cut` в
    `build_page_context` (тумблера тоже нет, экран появляется по условию). Заводить настройку
    ради «выключить блок, который и так не показывается без данных» — против правила проекта
    «менеджер настраивает нужное, а не всё подряд».

    JOIN на `users` — внутренний (как и у `game_block`): вопрос делегата, чья строка в `users`
    уже удалена, в блок не попадёт — это цена скоупа по городу/сезону. Журнал вопросов в боте
    (`list_questions_page`) делегата по-прежнему показывает — там LEFT JOIN, другая семантика.
    """
    parts, params = _scope_sql(conn, scope)

    total = _scalar(
        conn,
        "SELECT COUNT(*) FROM delegate_questions q "
        f"JOIN users ON users.telegram_id = q.user_id{_where(parts)}",
        params,
    ) or 0
    if total == 0:
        return None

    counts = {"new": 0, "in_work": 0, "answered": 0}
    status_sql = (
        f"SELECT {_QUESTION_STATUS_CASE} AS status, COUNT(*) AS cnt "
        "FROM delegate_questions q JOIN users ON users.telegram_id = q.user_id"
        f"{_where(parts)} GROUP BY 1"
    )
    for row in conn.execute(status_sql, params).fetchall():
        if row["status"] in counts:
            counts[row["status"]] = row["cnt"]

    answered = counts["answered"]
    answered_share = round(answered / total * 100, 1) if total else None
    waiting_now = counts["new"] + counts["in_work"]

    avg_answer_minutes = _avg_question_answer_minutes(conn, parts, params)

    # `ORDER BY julianday(q.asked_at) ASC LIMIT 1`, а НЕ `MIN(q.asked_at)`: `MIN` на TEXT-колонке
    # сравнивает строки побайтово, а формат разделителя даты/времени в проекте не один и тот же
    # (ISO «T» у текущего кода, легаси-строка с пробелом у старых записей) — при СОВПАДАЮЩЕЙ дате
    # пробел (0x20) лексикографически МЕНЬШЕ «T» (0x54), и `MIN` мог бы выбрать более новую
    # легаси-строку вместо реально самой старой ISO-строки того же дня. `julianday()` сравнивает
    # РАЗОБРАННОЕ время, а не байты, поэтому не подвержен этой ловушке (тот же приём уже
    # используется для отсечки в `_avg_question_answer_minutes`/`_avg_processing_minutes`).
    oldest_waiting_raw = _scalar(
        conn,
        "SELECT q.asked_at FROM delegate_questions q "
        f"JOIN users ON users.telegram_id = q.user_id{_where(parts + ['q.delivered_at IS NULL'])} "
        "ORDER BY julianday(q.asked_at) ASC LIMIT 1",
        params,
    )
    oldest_waiting_minutes = None
    if oldest_waiting_raw:
        try:
            asked = datetime.fromisoformat(oldest_waiting_raw)
        except (ValueError, TypeError):
            # Битый штамп -- не роняем страницу, просто нечем посчитать возраст (тот же
            # fail-soft, что _month_label/game_block.pending_oldest_minutes).
            asked = None
        if asked is not None:
            # D-4: возраст самого старого считаем от datetime.utcnow(), а НЕ msk_now(),
            # как соседний game_block. Квик 260912-mcj перевёл game_submissions.submitted_at
            # на московский msk_now() (handlers/user_actions.py) -- у game_block это по-прежнему
            # верно (обе стороны сравнения теперь на одних часах). У вопросов штампы остаются
            # UTC (create_question/claim_question/set_question_answer пишут
            # datetime.utcnow().isoformat()) -- разница с московским "сейчас" завысила бы
            # ожидание на смещение таймзоны (+3 ч). Копировать msk_now() у game_block здесь
            # НЕЛЬЗЯ -- это разные семьи меток времени.
            oldest_waiting_minutes = round(
                (datetime.utcnow() - asked).total_seconds() / 60.0, 1
            )

    top_sql = (
        "SELECT COALESCE(NULLIF(TRIM(q.answered_by_name), ''), '') AS name, "
        "COUNT(*) AS answered "
        "FROM delegate_questions q JOIN users ON users.telegram_id = q.user_id"
        f"{_where(parts + ['q.delivered_at IS NOT NULL'])} "
        "GROUP BY name ORDER BY answered DESC, name ASC LIMIT ?"
    )
    top_rows = conn.execute(top_sql, params + (_QUESTIONS_TOP_MANAGERS_LIMIT,)).fetchall()
    top_managers = [
        {"name": row["name"] or "без имени", "answered": row["answered"]}
        for row in top_rows
    ]

    return {
        "total": total,
        "new": counts["new"],
        "in_work": counts["in_work"],
        "answered": answered,
        "answered_share": answered_share,
        "waiting_now": waiting_now,
        "avg_answer_minutes": avg_answer_minutes,
        "avg_answer_label": format_processing_time(avg_answer_minutes),
        "oldest_waiting_minutes": oldest_waiting_minutes,
        "oldest_waiting_label": format_processing_time(oldest_waiting_minutes),
        "top_managers": top_managers,
    }


# ── Квик 260914-rgr (RGR-01..07): страница «Чат» ─────────────────────────────────────────
#
# Повтор `database.db.CHAT_PRESENT_STATUSES` — этот модуль ничего из `database/db.py` не
# импортирует (см. докстринг наверху файла, D-17: read-only периметр, отдельный процесс).
# Дрейф между копиями ловит `tests/test_dashboard_chat_260914.py`.
_CHAT_PRESENT_STATUSES = ("creator", "administrator", "member")
_CHAT_PER_CITY_SEP = "__city__"


def chat_bindings(conn) -> list[dict]:
    """Список привязанных чатов: `{"city": код|None, "label": подпись города/«Общий чат»,
    "chat_id": int, "title": название чата из группы}`. Мусорное/неразбираемое значение
    (не целое число) пропускается — тот же приём, что у `services.chat_tracking.bound_chats`
    на стороне бота (независимая копия — read-only процесс своей БД не пишет)."""
    rows = conn.execute(
        "SELECT key, value FROM bot_settings WHERE key LIKE 'delegate_chat_id%'"
    ).fetchall()
    out: list[dict] = []
    prefix = f"delegate_chat_id{_CHAT_PER_CITY_SEP}"
    for row in rows:
        key, value = row["key"], row["value"]
        if not value:
            continue
        try:
            chat_id = int(value)
        except (TypeError, ValueError):
            continue
        if key == "delegate_chat_id":
            code = None
        elif key.startswith(prefix):
            code = key[len(prefix):]
            if not code:
                continue
        else:
            continue  # чужой ключ, случайно попавший под LIKE (не должно бывать, но не наш)
        title_key = "delegate_chat_title" if code is None else f"delegate_chat_title{_CHAT_PER_CITY_SEP}{code}"
        title_row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (title_key,)
        ).fetchone()
        title = title_row["value"] if title_row is not None and title_row["value"] else None
        if code is None:
            label = "Общий чат"
        else:
            city_row = conn.execute("SELECT label FROM cities WHERE code = ?", (code,)).fetchone()
            label = city_row["label"] if city_row is not None else code
        out.append({"city": code, "label": label, "chat_id": chat_id, "title": title or label})
    return out


def chat_last_sync_at(conn, chat_id: int) -> str | None:
    """Дата последней сверки/события по этому чату — MAX(`updated_at`) по `chat_members`.
    Независимая read-only копия `database.db.chat_last_sync_at` (тот же приём, что у
    `_CHAT_PRESENT_STATUSES` выше в этом файле) — модуль не импортирует `database/db.py`
    (D-17: read-only периметр, отдельный процесс)."""
    row = conn.execute(
        "SELECT MAX(updated_at) AS last_sync FROM chat_members WHERE chat_id = ?", (chat_id,),
    ).fetchone()
    return row["last_sync"] if row is not None else None


def _chat_scope_ok(scope: Scope, chat: dict) -> bool:
    """Менеджер, привязанный к городу (`scope.city` задан), не должен получить числа чужого
    чата — даже если что-то вызовет эту функцию мимо уже отфильтрованного списка чатов."""
    return scope.city is None or chat["city"] == scope.city


def chat_overview(conn, scope: Scope, chat: dict) -> dict:
    """Одобрено / в чате / не в чате / в чате, но не зарегистрированы — для ОДНОГО чата.
    Городской фрагмент — тот же `_city_fragment`, что у остальных запросов."""
    if not _chat_scope_ok(scope, chat):
        return {"approved": 0, "in_chat": 0, "not_in_chat": 0, "unknown_members": 0}
    city_frag, city_params = ("", []) if chat["city"] is None else _city_fragment(conn, chat["city"])
    where_city = f" AND {city_frag}" if city_frag else ""
    present_ph = ",".join("?" for _ in _CHAT_PRESENT_STATUSES)

    approved = _scalar(
        conn, f"SELECT COUNT(*) FROM users u WHERE u.status = 'approved'{where_city}", city_params,
    ) or 0
    in_chat = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users u WHERE u.status = 'approved'{where_city} AND EXISTS "
        "(SELECT 1 FROM chat_members cm WHERE cm.telegram_id = u.telegram_id AND "
        f"cm.chat_id = ? AND cm.status IN ({present_ph}))",
        (*city_params, chat["chat_id"], *_CHAT_PRESENT_STATUSES),
    ) or 0
    unknown_members = _scalar(
        conn,
        f"SELECT COUNT(*) FROM chat_members cm WHERE cm.chat_id = ? AND cm.status IN "
        f"({present_ph}) AND NOT EXISTS (SELECT 1 FROM users u WHERE u.telegram_id = cm.telegram_id)",
        (chat["chat_id"], *_CHAT_PRESENT_STATUSES),
    ) or 0
    return {
        "approved": approved, "in_chat": in_chat,
        "not_in_chat": approved - in_chat, "unknown_members": unknown_members,
    }


def chat_joins_daily(conn, chat_id: int) -> list[tuple[str, int]]:
    """Плотный календарь вступлений по дням — из `chat_events` (`event = 'join'`), тот же
    приём заполнения дыр, что `daily_registrations`."""
    rows = conn.execute(
        "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS cnt FROM chat_events "
        "WHERE chat_id = ? AND event = 'join' GROUP BY day ORDER BY day ASC",
        (chat_id,),
    ).fetchall()
    sparse = [(row["day"], row["cnt"]) for row in rows]
    return _fill_missing_days(sparse)


def chat_messages_daily(conn, chat_id: int) -> list[tuple[str, int]]:
    """Плотный календарь сообщений по дням — `SUM(messages)` из `chat_activity`."""
    rows = conn.execute(
        "SELECT day, SUM(messages) AS cnt FROM chat_activity WHERE chat_id = ? "
        "GROUP BY day ORDER BY day ASC",
        (chat_id,),
    ).fetchall()
    sparse = [(row["day"], row["cnt"]) for row in rows]
    return _fill_missing_days(sparse)


def chat_not_joined(conn, scope: Scope, chat: dict, limit: int = 200) -> list[dict]:
    """Одобренные без присутствия в чате — telegram_id, город, дата одобрения, новые сверху.

    Отклонение от планового текста задачи («ФИО, @ник, город, дата одобрения»): имя и ник —
    персональные данные делегата, а `dashboard/queries.py` — единственный модуль дашборда с
    ЖЁСТКИМ структурным сторожем «без ПД» (D-17, `tests/test_dashboard_queries.py::
    test_queries_module_never_selects_pii_columns`, сканирует исходник на колонки-персоналии
    из карточки заявки). Дашборд — отдельный веб-периметр за Cloudflare Tunnel для держателей
    права `stats` (шире, чем доступ к самому боту) — персоналии туда осознанно не пускают ни
    в одном другом запросе модуля. Числовой telegram-идентификатор персоналией не считается
    (уже используется дашбордом как identity сессии) — менеджер находит человека по нему в
    самом боте («📇 Список заявок»), где персоналии уже показываются штатно."""
    if not _chat_scope_ok(scope, chat):
        return []
    city_frag, city_params = ("", []) if chat["city"] is None else _city_fragment(conn, chat["city"])
    where_city = f" AND {city_frag}" if city_frag else ""
    present_ph = ",".join("?" for _ in _CHAT_PRESENT_STATUSES)
    rows = conn.execute(
        "SELECT u.telegram_id, u.event_city, u.approved_at FROM users u "
        f"WHERE u.status = 'approved'{where_city} AND NOT EXISTS (SELECT 1 FROM chat_members cm "
        "WHERE cm.telegram_id = u.telegram_id AND cm.chat_id = ? AND cm.status IN "
        f"({present_ph})) ORDER BY u.approved_at DESC LIMIT ?",
        (*city_params, chat["chat_id"], *_CHAT_PRESENT_STATUSES, limit),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        city_code = row["event_city"]
        city_label_text = None
        if city_code:
            city_row = conn.execute("SELECT label FROM cities WHERE code = ?", (city_code,)).fetchone()
            city_label_text = city_row["label"] if city_row is not None else city_code
        out.append({
            "telegram_id": row["telegram_id"],
            "city": city_label_text,
            "approved_at": row["approved_at"],
        })
    return out


def chat_activity_leaderboard(conn, scope: Scope, chat: dict, limit: int = 50) -> list[dict]:
    """Рейтинг активности делегатов в чате — агрегаты по `chat_activity` (всё время), только
    делегаты из скоупа города/сезона. Сортировка: сначала по сообщениям (по убыванию), затем
    по replies. Подпись участника: @username (из `users.username`) или telegram_id, если ник
    отсутствует — та же дисциплина D-17, что `chat_not_joined` (без ФИО и контактов из анкеты).

    Поля строки:
    - `display_name`: @username или `telegram_id` — то, что видит менеджер в таблице;
    - `messages`: сумма сообщений за всё время;
    - `replies`: сумма ответов (replies) за всё время;
    - `media`: сумма сообщений с медиа;
    - `reply_rate`: процент ответов от messages (0..100, округлено до целого), None если
      messages=0 (защита от деления на 0).

    Фильтр города/сезона: только telegram_id из `users` в `scope` попадают в агрегацию.
    Привязка чата к городу (`chat["city"]`) учтена через `scope` — `build_chat_context` уже
    передаёт отфильтрованные карточки по `viewer["bound_city"]`. Дополнительный `_chat_scope_ok`
    остаётся страховочным, как у `chat_not_joined`."""
    if not _chat_scope_ok(scope, chat):
        return []
    parts, params = _scope_sql(conn, scope)
    # Собираем telegram_id делегатов в скоупе, чтобы ограничить агрегацию chat_activity
    user_ids_sql = f"SELECT telegram_id FROM users{_where(parts)}"
    rows = conn.execute(
        f"SELECT ca.telegram_id, u.username, "
        "SUM(ca.messages) AS messages, SUM(ca.replies) AS replies, SUM(ca.media) AS media "
        "FROM chat_activity ca "
        "JOIN users u ON u.telegram_id = ca.telegram_id "
        f"WHERE ca.chat_id = ? AND ca.telegram_id IN ({user_ids_sql}) "
        "GROUP BY ca.telegram_id, u.username "
        "ORDER BY messages DESC, replies DESC LIMIT ?",
        (chat["chat_id"], *params, limit),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        username = row["username"]
        telegram_id = row["telegram_id"]
        messages = row["messages"] or 0
        replies = row["replies"] or 0
        media = row["media"] or 0
        reply_rate = round(replies / messages * 100) if messages > 0 else None
        # В users.username бот пишет «@ник» или «-» без ника (handlers/registration.py).
        nick = (username or "").strip().lstrip("@")
        display_name = f"@{nick}" if nick and nick != "-" else str(telegram_id)
        out.append({
            "display_name": display_name,
            "messages": messages,
            "replies": replies,
            "media": media,
            "reply_rate": reply_rate,
        })
    return out


# ── рефералы (задача владельца «считать в дашборде инфу по рефералкам») ─────────────────────
#
# Источник — ИСКЛЮЧИТЕЛЬНО `users.referrer_id` (поданные заявки). Ни `reg_started`, ни
# `reg_drafts` не хранят `referrer_id` отдельной колонкой (`reg_drafts.meta` теоретически может
# нести его внутри JSON, но парсить построчно ради незавершённых анкет — риск без ценности:
# бросивший анкету делегат ещё не выбрал ни город, ни сезон, посчитать его в СКОУПЕ страницы
# нечем) — незавершённые сюда осознанно не попадают, только поданные заявки.
#
# Подпись пригласившего — telegram-ник (`users.username`), НЕ ФИО из анкеты: ту колонку этот
# модуль структурно не читает (D-17, test_queries_module_never_selects_pii_columns). Без ника —
# показываем telegram_id (тот же приём, что у `chat_not_joined`).

_REFERRAL_TOP_LIMIT = 20

_REFERRAL_STATUS_LABELS = {
    "pending": "На модерации",
    "approved": "Одобрено",
    "rejected": "Отклонено",
}


def _referral_status_label(status: "str | None") -> str:
    if not status:
        return "—"
    return _REFERRAL_STATUS_LABELS.get(status, status)


def referral_summary(conn, scope: Scope) -> dict:
    """Итоги блока «Рефералы» в скоупе (город+сезон): сколько заявок пришло по ссылкам (и
    какая это доля от всех заявок скоупа), их статусы, сколько делегатов имеют свою ссылку
    (`is_ambassador`) и сколько ответили «да» на вопрос об амбассадорстве
    (`is_ambassador_candidate`), сколько человек привели хотя бы одного делегата."""
    parts, params = _scope_sql(conn, scope)
    total_all = _scalar(conn, f"SELECT COUNT(*) FROM users{_where(parts)}", params) or 0

    ref_parts = parts + ["referrer_id IS NOT NULL"]
    referred_total = _scalar(
        conn, f"SELECT COUNT(*) FROM users{_where(ref_parts)}", params
    ) or 0

    statuses = {"pending": 0, "approved": 0, "rejected": 0}
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS cnt FROM users{_where(ref_parts)} GROUP BY status", params
    ).fetchall()
    for row in rows:
        if row["status"] in statuses:
            statuses[row["status"]] = row["cnt"]

    ambassadors = _scalar(
        conn, f"SELECT COUNT(*) FROM users{_where(parts + ['is_ambassador = 1'])}", params
    ) or 0
    candidates = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + ['is_ambassador_candidate = 1'])}",
        params,
    ) or 0
    inviters = _scalar(
        conn, f"SELECT COUNT(DISTINCT referrer_id) FROM users{_where(ref_parts)}", params
    ) or 0

    return {
        "total_all": total_all,
        "referred_total": referred_total,
        "referred_share": (
            round(referred_total / total_all * 100, 1) if total_all else None
        ),
        "referred_pending": statuses["pending"],
        "referred_approved": statuses["approved"],
        "referred_rejected": statuses["rejected"],
        "ambassadors": ambassadors,
        "candidates": candidates,
        "inviters": inviters,
    }


def referral_top(conn, scope: Scope) -> list[dict]:
    """Топ-`_REFERRAL_TOP_LIMIT` пригласивших — группировка по `referrer_id` СРЕДИ
    приглашённых В СКОУПЕ страницы (город+сезон приглашённого, а не самого пригласившего:
    воронка про того, КОГО привели). Пригласивший может быть удалён из `users`
    (`referrer_id` без соответствующей строки) — такая строка получает `deleted=True` вместо
    падения (аналог LEFT JOIN, только вторым запросом — тот же приём, что у `city_comparison`,
    который тоже резолвит справочник по одному коду за раз)."""
    parts, params = _scope_sql(conn, scope)
    ref_parts = parts + ["referrer_id IS NOT NULL"]
    sql = (
        "SELECT referrer_id, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved "
        f"FROM users{_where(ref_parts)} GROUP BY referrer_id "
        "ORDER BY total DESC, referrer_id ASC LIMIT ?"
    )
    rows = conn.execute(sql, params + (_REFERRAL_TOP_LIMIT,)).fetchall()

    result: list[dict] = []
    for row in rows:
        referrer_id = row["referrer_id"]
        referrer = conn.execute(
            "SELECT username, event_city, status, is_ambassador FROM users WHERE telegram_id = ?",
            (referrer_id,),
        ).fetchone()
        if referrer is None:
            result.append({
                "telegram_id": referrer_id,
                "username": None,
                "deleted": True,
                "city": None,
                "status_label": "—",
                "is_ambassador": False,
                "total": row["total"],
                "approved": row["approved"] or 0,
            })
            continue
        city_code = referrer["event_city"]
        city_label = None
        if city_code:
            city_row = conn.execute(
                "SELECT label FROM cities WHERE code = ?", (city_code,)
            ).fetchone()
            city_label = city_row["label"] if city_row is not None else city_code
        result.append({
            "telegram_id": referrer_id,
            "username": referrer["username"],
            "deleted": False,
            "city": city_label,
            "status_label": _referral_status_label(referrer["status"]),
            "is_ambassador": bool(referrer["is_ambassador"]),
            "total": row["total"],
            "approved": row["approved"] or 0,
        })
    return result


def referral_daily(conn, scope: Scope) -> list[tuple[str, int]]:
    """Плотный календарь регистраций по реф-ссылкам — тот же приём заполнения дыр, что
    `daily_registrations`, сужено `referrer_id IS NOT NULL`."""
    parts, params = _scope_sql(conn, scope)
    date_parts = parts + [
        "referrer_id IS NOT NULL",
        "registration_date IS NOT NULL",
        "TRIM(registration_date) != ''",
    ]
    rows = conn.execute(
        "SELECT substr(registration_date, 1, 10) AS day, COUNT(*) AS cnt FROM users"
        f"{_where(date_parts)} GROUP BY day ORDER BY day ASC",
        params,
    ).fetchall()
    sparse = [(row["day"], row["cnt"]) for row in rows]
    return _fill_missing_days(sparse)


def referral_city_breakdown(conn, scope: Scope) -> list[tuple[str, int]]:
    """Разрез приглашённых по ссылке по городам мероприятия — только когда включён модуль
    городов (`event_city_enabled`, тот же гейт, что у `breakdown('event_city', …)`) и страница
    смотрит на «все города» (`scope.city is None`) — менеджеру, привязанному к своему городу,
    разрез по городам самого себя ничего не добавляет (D-10 показывает такому менеджеру только
    его город целиком)."""
    flags = dashboard_flags(conn)
    if flags.get("event_city_enabled") != "on" or scope.city is not None:
        return []
    parts, params = _scope_sql(conn, scope)
    city_parts = parts + [
        "referrer_id IS NOT NULL",
        "event_city IS NOT NULL", "TRIM(event_city) != ''", "event_city != '-'",
    ]
    rows = conn.execute(
        "SELECT event_city AS code, COUNT(*) AS cnt FROM users"
        f"{_where(city_parts)} GROUP BY event_city ORDER BY cnt DESC",
        params,
    ).fetchall()
    result: list[tuple[str, int]] = []
    for row in rows:
        city_row = conn.execute(
            "SELECT label FROM cities WHERE code = ?", (row["code"],)
        ).fetchone()
        label = city_row["label"] if city_row is not None else row["code"]
        result.append((label, row["cnt"]))
    return result


def referral_block(conn, scope: Scope) -> dict | None:
    """`None`, если тумблер `dashboard_block_referrals` выключен ИЛИ в скоупе странице
    нечего показать (нет ни одной заявки по ссылке, ни амбассадоров, ни кандидатов) — та же
    семантика «тумблер + наличие данных», что у `game_block`/`questions_block`."""
    flags = dashboard_flags(conn)
    if flags.get("dashboard_block_referrals") != "on":
        return None

    summary = referral_summary(conn, scope)
    if not summary["referred_total"] and not summary["ambassadors"] and not summary["candidates"]:
        return None

    daily_rows = referral_daily(conn, scope)
    return {
        "summary": summary,
        "top": referral_top(conn, scope),
        "daily": daily_rows,
        "city_cut": referral_city_breakdown(conn, scope),
    }


# ── амбассадоры и волны (D-33/D-34) ──────────────────────────────────────────────────────
#
# Формулы — `.planning/phases/32-ambassador-waves/32-RESEARCH-DOMAIN.md`, раздел «Metric
# Definitions (D-33)». Схема — план 32-01 (`ambassador_waves`/`wave_results`/
# `referral_credits`, колонки `game_tasks.wave_id`/`audience`, `coins.task_id`,
# `users.is_ambassador`/`ambassador_since`). Сервис `services/ambassador_waves.py`
# (параллельный план 32-03) считает ТЕ ЖЕ метрики для бота — держать в синхроне с этим модулем
# при изменении формул; здесь — независимая read-only реализация (дашборд не импортирует
# сервисы бота, см. модульный докстринг файла), сверка чисел — тест
# `test_ambassador_block_matches_domain_fixture` на общей фикстуре.

_AMBASSADOR_ROWS_LIMIT = 20

# Копия `database.db.NO_DEADLINE_AT` — модуль НЕ импортирует `database.db` (см. докстринг
# файла), дрейф ловит `test_ambassador_no_deadline_sentinel_matches_bot_db`.
_NO_DEADLINE_AT = "9999-12-31 23:59:59"


def _ambassador_current_wave(conn, scope: Scope) -> "dict | None":
    """Волна, в чьи даты попадает «сейчас» (МСК); если такой нет — последняя ОБЪЯВЛЕННАЯ
    волна скоупа. Единственная таблица в запросе — `ambassador_waves`, колонка `event_city`
    не квалифицируется (`_user_scoped_parts` здесь не нужен, ни с чем не джойнимся).

    Фикс WR-14 (фаза 32): `state != 'draft'` — та же оговорка, что у бота
    (`database.db.wave_at`, «состояние не 'draft'»). Без неё «Скопировать прошлую» с датами,
    захватывающими текущий момент, подставляла дашборду ещё не запущенный черновик — на
    боевом рейтинге волны амбассадоров бота эта волна не видна вовсе."""
    city_frag, city_params = _city_sql(conn, scope.city)
    parts = [city_frag] if city_frag else []
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    state_not_draft = "state != 'draft'"
    row = conn.execute(
        f"SELECT * FROM ambassador_waves{_where(parts + ['starts_at <= ?', 'ends_at >= ?', state_not_draft])} "
        "ORDER BY starts_at DESC LIMIT 1",
        tuple(city_params) + (now, now),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT * FROM ambassador_waves{_where(parts + ['state = ?'])} "
            "ORDER BY ends_at DESC LIMIT 1",
            tuple(city_params) + ("announced",),
        ).fetchone()
    return dict(row) if row is not None else None


def _ambassador_funnel(conn, scope: Scope) -> list[dict]:
    """Воронка приглашённых на амбассадора: подали / одобрены / оплатили / уже принесли
    баллы (`referral_credits`). Группировка одним запросом по ВСЕМ `referrer_id` (без городского
    скоупа приглашённого — воронка про амбассадора, не про город приглашённого), городской скоуп
    применяется только к списку самих амбассадоров. Сортировка по числу одобренных, потолок —
    `_AMBASSADOR_ROWS_LIMIT`.

    IN-08 (32-REVIEW.md): приглашённый фильтруется СВОИМ сезоном (`_season_sql`, D-13 —
    `season=None` значит текущий) — раньше воронка считала приглашённых ВСЕХ сезонов, и
    амбассадор, приглашавший людей в прошлом сезоне, показывал завышенную воронку в разрезе
    текущего события. Городской скоуп на приглашённого по-прежнему не накладывается (см. абзац
    выше) — только сезон."""
    parts, params = _scope_sql(conn, scope)
    ambassadors = conn.execute(
        f"SELECT telegram_id, username FROM users{_where(parts + ['is_ambassador = 1'])}",
        params,
    ).fetchall()
    if not ambassadors:
        return []
    amb_usernames = {row["telegram_id"]: row["username"] for row in ambassadors}

    season_frag, season_params = _season_sql(conn, scope.season)
    funnel_rows = conn.execute(
        "SELECT referrer_id, COUNT(*) AS submitted, "
        "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved, "
        "SUM(CASE WHEN payment_status = 'paid' THEN 1 ELSE 0 END) AS paid "
        f"FROM users{_where(['referrer_id IS NOT NULL', season_frag])} GROUP BY referrer_id",
        tuple(season_params),
    ).fetchall()
    funnel_by_id = {row["referrer_id"]: row for row in funnel_rows}

    credited_rows = conn.execute(
        "SELECT rc.referrer_id, COUNT(*) AS credited FROM referral_credits rc "
        f"JOIN users u ON u.telegram_id = rc.invitee_id{_where([season_frag])} "
        "GROUP BY rc.referrer_id",
        tuple(season_params),
    ).fetchall()
    credited_by_id = {row["referrer_id"]: row["credited"] for row in credited_rows}

    result = []
    for telegram_id, username in amb_usernames.items():
        row = funnel_by_id.get(telegram_id)
        result.append({
            "telegram_id": telegram_id,
            "username": username,
            "submitted": row["submitted"] if row else 0,
            "approved": (row["approved"] or 0) if row else 0,
            "paid": (row["paid"] or 0) if row else 0,
            "credited": credited_by_id.get(telegram_id, 0),
        })
    result.sort(key=lambda r: (-r["approved"], r["telegram_id"]))
    return result[:_AMBASSADOR_ROWS_LIMIT]


def _ambassador_wave_results_snapshot(conn, wave: dict) -> list[dict]:
    """Фикс WR-14 (фаза 32): волна `announced` — читаем неизменяемый снимок призёров
    `wave_results` (D-17), а НЕ пересчитываем по `coins`/`referral_credits`. Сдача, проверенная
    уже ПОСЛЕ объявления, продолжает пополнять общий зачёт (D-17), но не имеет права задним
    числом изменить то, что увидели амбассадоры в сообщении об итогах — живой пересчёт молча
    разошёлся бы с уже объявленными числами. Снимок хранит места ВСЕХ участников волны (призёры
    помечены `is_winner`), поэтому после объявления список той же длины, что и во время волны."""
    rows = conn.execute(
        "SELECT wr.user_id AS user_id, wr.points AS points, users.username AS username "
        "FROM wave_results wr JOIN users ON users.telegram_id = wr.user_id "
        "WHERE wr.wave_id = ? ORDER BY wr.place ASC",
        (wave["id"],),
    ).fetchall()
    return [
        {"telegram_id": row["user_id"], "username": row["username"], "points": row["points"]}
        for row in rows
    ]


def _ambassador_wave_rating(conn, scope: Scope, wave: "dict | None") -> list[dict]:
    """Рейтинг ТЕКУЩЕЙ волны: баллы = сумма `coins` по заданиям волны (привязка задания,
    не дата проверки, D-14а) + сумма `referral_credits` этой волны (D-14б). `[]`, если волны
    сейчас нет в скоупе.

    Фикс WR-14 (фаза 32): участвуют ТОЛЬКО те, кто проходит `wave_eligible` бота —
    `users.is_ambassador = 1` И (`ambassador_since` пусто ИЛИ не позже старта волны), иначе
    вышедший амбассадор или обычный делегат, сдавший задание волны с аудиторией «всем»,
    попадал на дашборд, которого нет в рейтинге бота. `c.source = 'task'` — та же граница, что
    у бота (`database.db.sum_task_coins_for_wave`), а не «любой положительный coins.delta»
    (ручная правка/штраф с отрицательным delta по тому же заданию раньше пропадали из суммы)."""
    if wave is None:
        return []
    if wave["state"] == "announced":
        return _ambassador_wave_results_snapshot(conn, wave)
    parts, params = _scope_sql(conn, scope)
    eligible = ["users.is_ambassador = 1", "(users.ambassador_since IS NULL OR users.ambassador_since <= ?)"]
    eligible_params = params + (wave["starts_at"],)
    source_is_task = "c.source = 'task'"

    task_sql = (
        "SELECT c.user_id AS user_id, SUM(c.delta) AS points FROM coins c "
        "JOIN users ON users.telegram_id = c.user_id "
        "JOIN game_tasks t ON t.id = c.task_id "
        f"{_where(_user_scoped_parts(parts) + eligible + ['t.wave_id = ?', source_is_task])} "
        "GROUP BY c.user_id"
    )
    points: dict[int, int] = {
        row["user_id"]: row["points"] or 0
        for row in conn.execute(task_sql, eligible_params + (wave["id"],)).fetchall()
    }

    ref_sql = (
        "SELECT rc.referrer_id AS user_id, SUM(rc.coins) AS points FROM referral_credits rc "
        "JOIN users ON users.telegram_id = rc.referrer_id "
        f"{_where(parts + eligible + ['rc.wave_id = ?'])} GROUP BY rc.referrer_id"
    )
    for row in conn.execute(ref_sql, eligible_params + (wave["id"],)).fetchall():
        points[row["user_id"]] = points.get(row["user_id"], 0) + (row["points"] or 0)

    if not points:
        return []
    placeholders = ", ".join("?" for _ in points)
    usernames = {
        row["telegram_id"]: row["username"]
        for row in conn.execute(
            f"SELECT telegram_id, username FROM users WHERE telegram_id IN ({placeholders})",
            tuple(points),
        ).fetchall()
    }
    rows = [
        {"telegram_id": uid, "username": usernames.get(uid), "points": pts}
        for uid, pts in points.items()
    ]
    rows.sort(key=lambda r: (-r["points"], r["telegram_id"]))
    return rows[:_AMBASSADOR_ROWS_LIMIT]


def _ambassador_lifetime_top(conn, scope: Scope) -> list[dict]:
    """Верхушка общего зачёта (D-15: весь `coins`, без фильтра по волне) среди амбассадоров
    скоупа страницы."""
    parts, params = _scope_sql(conn, scope)
    sql = (
        "SELECT c.user_id AS user_id, users.username AS username, SUM(c.delta) AS points "
        "FROM coins c JOIN users ON users.telegram_id = c.user_id "
        f"{_where(parts + ['users.is_ambassador = 1'])} "
        "GROUP BY c.user_id, users.username ORDER BY points DESC, c.user_id ASC LIMIT ?"
    )
    rows = conn.execute(sql, params + (_AMBASSADOR_ROWS_LIMIT,)).fetchall()
    return [
        {"telegram_id": row["user_id"], "username": row["username"], "points": row["points"] or 0}
        for row in rows
    ]


def _ambassador_past_winners(conn, scope: Scope) -> list[dict]:
    """Призёры прошлых волн из неизменяемого снимка `wave_results` (D-17), скоуп страницы
    квалифицирован к колонке-владельцу — `users.event_city`/`users.season` (победитель, не
    сама волна): волна и её призёры всегда одного города, поэтому это то же самое множество,
    но без второй ручной квалификации алиаса `w.`."""
    parts, params = _scope_sql(conn, scope)
    sql = (
        "SELECT w.number AS number, wr.place AS place, wr.points AS points, "
        "wr.user_id AS user_id, users.username AS username "
        "FROM wave_results wr "
        "JOIN ambassador_waves w ON w.id = wr.wave_id "
        "JOIN users ON users.telegram_id = wr.user_id"
        f"{_where(_user_scoped_parts(parts))} ORDER BY w.number DESC, wr.place ASC LIMIT ?"
    )
    rows = conn.execute(sql, params + (_AMBASSADOR_ROWS_LIMIT,)).fetchall()
    return [
        {
            "wave_number": row["number"], "place": row["place"], "points": row["points"],
            "telegram_id": row["user_id"], "username": row["username"],
        }
        for row in rows
    ]


def _ambassador_wave_tasks(conn, scope: Scope, wave: "dict | None") -> list[dict]:
    """По каждому заданию ТЕКУЩЕЙ волны: сдано всего / вовремя / с просрочкой. Задание с
    `deadline_at == _NO_DEADLINE_AT` (сентинел «без срока») — все его сдачи «вовремя» (план,
    формула D-33: «задание без срока попадает в вовремя, а не в просрочку»).

    IN-08 (32-REVIEW.md): отклонённая сдача (`status = 'rejected'`) — не сдача с точки зрения
    этой метрики (менеджер её отверг, задание фактически не выполнено) — раньше она всё равно
    попадала в «сдано» и, если пришла до дедлайна, в «вовремя», завышая оба числа."""
    if wave is None:
        return []
    parts, params = _scope_sql(conn, scope)
    not_rejected = "s.status != 'rejected'"
    sql = (
        "SELECT t.id AS id, t.title AS title, t.text AS text, t.deadline_at AS deadline_at, "
        "s.submitted_at AS submitted_at FROM game_submissions s "
        "JOIN users ON users.telegram_id = s.user_id "
        "JOIN game_tasks t ON t.id = s.task_id "
        f"{_where(_user_scoped_parts(parts) + ['t.wave_id = ?', not_rejected])}"
    )
    rows = conn.execute(sql, params + (wave["id"],)).fetchall()

    by_task: dict[int, dict] = {}
    for row in rows:
        entry = by_task.setdefault(row["id"], {
            "title": _task_title(row["title"], row["text"]),
            "submitted": 0, "on_time": 0, "late": 0,
        })
        entry["submitted"] += 1
        deadline = row["deadline_at"]
        if deadline == _NO_DEADLINE_AT or row["submitted_at"] <= deadline:
            entry["on_time"] += 1
        else:
            entry["late"] += 1

    result = []
    for entry in by_task.values():
        entry["on_time_share"] = (
            round(entry["on_time"] / entry["submitted"] * 100, 1) if entry["submitted"] else None
        )
        result.append(entry)
    result.sort(key=lambda r: r["submitted"], reverse=True)
    return result[:_AMBASSADOR_ROWS_LIMIT]


def ambassador_block(conn, scope: Scope) -> "dict | None":
    """`None`, если тумблер `dashboard_block_ambassadors` выключен ИЛИ в скоупе страницы нет
    ни одного амбассадора (та же семантика «тумблер + наличие данных», что у `game_block`/
    `referral_block`). Соединение только на чтение — ни одного INSERT/UPDATE (T-32-09-04)."""
    flags = dashboard_flags(conn)
    if flags.get("dashboard_block_ambassadors") != "on":
        return None

    parts, params = _scope_sql(conn, scope)
    total = _scalar(
        conn, f"SELECT COUNT(*) FROM users{_where(parts + ['is_ambassador = 1'])}", params
    ) or 0
    if total == 0:
        return None

    wave = _ambassador_current_wave(conn, scope)
    active = 0
    activation_share = None
    if wave is not None:
        active_sql = (
            "SELECT COUNT(DISTINCT s.user_id) FROM game_submissions s "
            "JOIN users ON users.telegram_id = s.user_id "
            "JOIN game_tasks t ON t.id = s.task_id "
            f"{_where(_user_scoped_parts(parts) + ['users.is_ambassador = 1', 't.wave_id = ?', 's.submitted_at >= ?', 's.submitted_at <= ?'])}"
        )
        active = _scalar(
            conn, active_sql, params + (wave["id"], wave["starts_at"], wave["ends_at"])
        ) or 0
        eligible_sql = (
            f"SELECT COUNT(*) FROM users{_where(parts + ['is_ambassador = 1', '(ambassador_since IS NULL OR ambassador_since <= ?)'])}"
        )
        eligible = _scalar(conn, eligible_sql, params + (wave["starts_at"],)) or 0
        activation_share = round(active / eligible * 100, 1) if eligible else None

    wave_view = None
    if wave is not None:
        # IN-08 (32-REVIEW.md): в режиме «все города» (`scope.city is None`) номер волны сам
        # по себе неоднозначен — у каждого города своя нумерация («Волна 2» Москвы и «Волна 2»
        # Петербурга — разные волны). Город показываем ТОЛЬКО когда скоуп не сужен и у волны
        # вообще есть свой город (волна «все города» — `event_city` пуст — города не имеет).
        wave_city = None
        if scope.city is None and wave.get("event_city"):
            city_row = conn.execute(
                "SELECT label FROM cities WHERE code = ?", (wave["event_city"],)
            ).fetchone()
            wave_city = city_row["label"] if city_row is not None else wave["event_city"]
        wave_view = {
            "number": wave["number"], "starts_at": wave["starts_at"], "ends_at": wave["ends_at"],
            "state": wave["state"], "city": wave_city,
        }

    return {
        "total": total,
        "wave": wave_view,
        "active": active,
        "activation_share": activation_share,
        "funnel": _ambassador_funnel(conn, scope),
        "wave_rating": _ambassador_wave_rating(conn, scope, wave),
        "lifetime": _ambassador_lifetime_top(conn, scope),
        "past_winners": _ambassador_past_winners(conn, scope),
        "tasks": _ambassador_wave_tasks(conn, scope, wave),
    }
