"""Конструктор «Источники по дням» (запрос менеджера DXP СПб, 27.09): сколько поданных
заявок пришло по каждому источнику в конкретный день или неделю — «сколько пришло от
амбассадоров 21 числа» без ручного подсчёта.

Одна функция `build(conn, scope, q)` параметризована фильтрами, разбивкой и шагом
(`SourcesQuery`) — таблица, график и CSV страницы `/sources` строятся из её результата,
поэтому расходиться между собой не могут.

Заявка = строка `users` с непустым `registration_date` (подача анкеты); день — первые 10
символов `registration_date`, а он с 12.09 пишется по Москве (`timeutil.msk_now` бота) — то
есть это московские сутки. Скоуп города/сезона — тот же `queries._scope_sql`, что у всей
главной страницы; признак «это метка ссылки, а не ответ человека» — тот же
`queries._UTM_TAG_PREDICATE`, что у блока «Метки кампаний». Своих правил здесь нет.

Строк немного (1–3 тыс. заявок за сезон), поэтому SQL только сужает скоуп и отдаёт по
строке на заявку, а корзины/фильтры собираются в Python — так правило «ИЛИ внутри группы,
И между группами» и хвост «Остальные» читаются в одном месте. Значения из URL в SQL не
попадают вовсе — только сравниваются в Python. ПД наружу не уходят: ни имён, ни ID — только
счётчики (D-17 дашборда); CSV — те же агрегаты, что видны на странице.

Разбивка «Канал» — единый ключ: у пришедшего по ссылке с меткой это «🔗 <метка>» (вопрос
«откуда узнал» ему не задаётся), иначе ответ из анкеты, иначе «Не указано». Ручные ответы,
отличающиеся только регистром и пробелами, склеены в одну корзину.
"""
from __future__ import annotations

import csv
import io
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from arrival_stats import _sheet_safe
from dashboard import queries
from dashboard.timeutil import msk_now

BREAKDOWNS = ("answer", "tag", "city", "status")
STEPS = ("day", "week")
PERIODS = ("all", "7", "30", "today", "yesterday")

BY_LABELS = {
    "answer": "Канал (метка ссылки или ответ в анкете)",
    "tag": "Ссылка, по которой пришёл",
    "city": "Город",
    "status": "Статус заявки",
}
STEP_LABELS = {"day": "По дням", "week": "По неделям"}
PERIOD_LABELS = {
    "today": "Сегодня", "yesterday": "Вчера", "7": "7 дней", "30": "30 дней", "all": "Весь сезон",
}

STATUS_LABELS = {"approved": "Одобрена", "pending": "Ждёт решения", "rejected": "Отказ"}
_STATUS_OTHER = "other"
_STATUS_OTHER_LABEL = "Другой статус"
# Смысловые цвета статусов: зелёный / жёлтый / коралловый (порядок токенов --chart-N).
_STATUS_COLORS = {"approved": "3", "pending": "4", "rejected": "6", _STATUS_OTHER: "other"}

# Дубль `reg_options.PARTY_TRACK_OPTIONS`: дашборд не тянет корневые модули бота без COPY в
# образ (tests/test_dashboard_docker.py). Дрейф ловит test_track_labels_match_bot_registry.
TRACK_LABELS = {
    "full": "Полная регистрация",
    "party_overnight": "\U0001f389 Гости с ночёвкой",
    "party_noovernight": "\U0001f389 Гости без ночёвки",
}

ANSWER_NOT_GIVEN = "Не указано"
# Старое значение корзины из поделённых ссылок: в from_params выкидывается из answers (молча
# отбросить проще алиаса «все метки», которому понадобилось бы отдельное правило в _matches).
_ANSWER_BY_TAG_OLD = "По метке ссылки"
_TAG_MARK = "🔗 "
TAG_NONE = "Без метки"
TAG_AMBASSADOR = "Личная ссылка амбассадора"
# Регистрация на месте (D-41): и walk-in, и одобренные у стойки — отдельная корзина разбивки
# «Ссылка, по которой пришёл», важнее метки и ссылки амбассадора.
TAG_ONSITE = "📍 На месте"
REST = "_rest"
REST_LABEL = "Остальные"

# 7 цветов палитры + серый «Остальные» (BRAND.md § Графики: не больше восьми).
_TOP_LIMIT = 7
_MAX_SPAN_DAYS = 731
_MAX_VALUES = 30
_MAX_VALUE_LEN = 200
# Отделы ведут метрики по каналам каждый день: неделя-две помещаются колонками матрицы,
# больше — нечитаемо, остаётся итог за период.
_MATRIX_MAX_DAYS = 14
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


# ── состояние конструктора в URL ─────────────────────────────────────────────────────────

def _clean_list(values) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in values:
        value = (raw or "").strip()[:_MAX_VALUE_LEN]
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= _MAX_VALUES:
            break
    return tuple(seen)


def _parse_day(value) -> "date | None":
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class SourcesQuery:
    by: str = "answer"
    step: str = "day"
    statuses: tuple = field(default_factory=tuple)
    answers: tuple = field(default_factory=tuple)
    tags: tuple = field(default_factory=tuple)
    tracks: tuple = field(default_factory=tuple)
    ambassador_only: bool = False
    period: str = "all"
    date_from: "date | None" = None
    date_to: "date | None" = None

    @classmethod
    def from_params(cls, params) -> "SourcesQuery":
        """Мусор в URL (чужая разбивка, неизвестный статус, кривая дата) молча
        отбрасывается — страница открывается с разумным значением, а не падает."""
        by = params.get("by") or "answer"
        step = params.get("step") or "day"
        period = params.get("period") or "all"
        date_from = _parse_day(params.get("from"))
        date_to = _parse_day(params.get("to"))
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        return cls(
            by=by if by in BREAKDOWNS else "answer",
            step=step if step in STEPS else "day",
            statuses=tuple(s for s in _clean_list(params.getlist("status")) if s in STATUS_LABELS),
            answers=tuple(v for v in _clean_list(params.getlist("src")) if v != _ANSWER_BY_TAG_OLD),
            tags=_clean_list(params.getlist("tag")),
            tracks=_clean_list(params.getlist("track")),
            ambassador_only=params.get("amb") == "1",
            period=period if period in PERIODS else "all",
            date_from=date_from,
            date_to=date_to,
        )

    @property
    def custom_range(self) -> bool:
        return self.date_from is not None or self.date_to is not None

    @property
    def has_filters(self) -> bool:
        return bool(
            self.statuses or self.answers or self.tags or self.tracks or self.ambassador_only
            or self.custom_range or self.period != "all"
        )

    def toggled(self, group: str, value: str) -> "SourcesQuery":
        attr = {"status": "statuses", "src": "answers", "tag": "tags", "track": "tracks"}[group]
        current = getattr(self, attr)
        if group == "src":
            # «вк» в URL и клик по чипу «ВК» — одно значение: снимаем, а не дублируем
            same = tuple(v for v in current if _match_key(v) == _match_key(value))
            new = tuple(v for v in current if v not in same) if same else current + (value,)
        else:
            new = tuple(v for v in current if v != value) if value in current else current + (value,)
        return replace(self, **{attr: new})

    def pairs(self, *, city, season) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if city:
            out.append(("city", city))
        if season:
            out.append(("season", season))
        if self.by != "answer":
            out.append(("by", self.by))
        if self.step != "day":
            out.append(("step", self.step))
        out += [("status", v) for v in self.statuses]
        out += [("src", v) for v in self.answers]
        out += [("tag", v) for v in self.tags]
        out += [("track", v) for v in self.tracks]
        if self.ambassador_only:
            out.append(("amb", "1"))
        if self.custom_range:
            if self.date_from:
                out.append(("from", self.date_from.isoformat()))
            if self.date_to:
                out.append(("to", self.date_to.isoformat()))
        elif self.period != "all":
            out.append(("period", self.period))
        return out

    def href(self, *, city, season, toggle=None, **overrides) -> str:
        q = replace(self, **overrides) if overrides else self
        if toggle is not None:
            q = q.toggled(*toggle)
        return "?" + urllib.parse.urlencode(q.pairs(city=city, season=season))


# ── выборка и корзины ────────────────────────────────────────────────────────────────────

def _is_blank(value) -> bool:
    return value is None or not str(value).strip() or str(value).strip() == "-"


def _norm(text) -> str:
    return " ".join(str(text).split()).casefold()


def _match_key(value: str) -> str:
    """Метки ссылок — slug, их не склеиваем; ручные ответы сравниваем без регистра/пробелов."""
    if value.startswith(_TAG_MARK) or value == REST:
        return value
    return _norm(value)


def _answer_key(row) -> str:
    if row["is_tag"]:
        return _TAG_MARK + str(row["source"]).strip()
    if _is_blank(row["source"]):
        return ANSWER_NOT_GIVEN
    return str(row["source"]).strip()


def _tag_key(row) -> str:
    if row.get("onsite"):
        return TAG_ONSITE
    if row["is_tag"]:
        return row["source"]
    if row["amb"]:
        return TAG_AMBASSADOR
    return TAG_NONE


def _status_key(row) -> str:
    return row["status"] if row["status"] in STATUS_LABELS else _STATUS_OTHER


def _fetch(conn, scope: queries.Scope) -> list[dict]:
    parts, params = queries._scope_sql(conn, scope)
    parts = parts + ["registration_date IS NOT NULL", "TRIM(registration_date) != ''"]
    tag_expr = " AND ".join(queries._UTM_TAG_PREDICATE)
    # Колонку заводит бот при старте; дашборд, поднятый раньше бота, читает старую схему.
    has_onsite = any(r["name"] == "onsite_kind" for r in conn.execute("PRAGMA table_info(users)"))
    # «На месте» — только короткая анкета у стойки (walk-in). Одобренный у стойки (door) пришёл
    # своим каналом раньше — задним числом он из корзины своей ссылки/метки не уходит.
    onsite_expr = "CASE WHEN onsite_kind = 'walkin' THEN 1 ELSE 0 END" if has_onsite else "0"
    rows = conn.execute(
        "SELECT substr(registration_date, 1, 10) AS day, source, "
        f"CASE WHEN {tag_expr} THEN 1 ELSE 0 END AS is_tag, "
        "CASE WHEN referrer_id IS NOT NULL THEN 1 ELSE 0 END AS amb, "
        f"status, participant_type, event_city, {onsite_expr} AS onsite FROM users"
        f"{queries._where(parts)}",
        params,
    ).fetchall()

    default_city = queries._default_city_code(conn)
    known = set(queries._known_city_codes(conn))
    result: list[dict] = []
    for row in rows:
        day = _parse_day(row["day"])
        if day is None:
            continue  # битая дата — не роняем страницу, как _fill_missing_days
        item = {
            "day": day,
            "source": row["source"],
            "is_tag": bool(row["is_tag"]),
            "amb": bool(row["amb"]),
            "onsite": bool(row["onsite"]),
            "status": row["status"],
            "track": row["participant_type"] or "full",
            "city": row["event_city"] if row["event_city"] in known else default_city,
        }
        item["answer"] = _answer_key(item)
        item["tag"] = _tag_key(item)
        item["status_key"] = _status_key(item)
        result.append(item)
    _canonicalize_answers(result)
    return result


def _canonicalize_answers(rows: list[dict]) -> None:
    """Склеивает ручные ответы, отличающиеся только регистром/пробелами, в самое частое
    написание (при равенстве — лексикографически первое). Считаем по всему скоупу, до
    периода и фильтров: показываемое написание = ключ в URL и не должно меняться при
    переключении периода."""
    groups: dict[str, Counter] = {}
    for r in rows:
        if r["is_tag"] or r["answer"] == ANSWER_NOT_GIVEN:
            continue
        spelling = " ".join(r["answer"].split())
        groups.setdefault(_norm(spelling), Counter())[spelling] += 1
    best = {
        k: min(c, key=lambda sp: (-c[sp], sp)) for k, c in groups.items()
    }
    for r in rows:
        if r["is_tag"] or r["answer"] == ANSWER_NOT_GIVEN:
            continue
        r["answer"] = best[_norm(r["answer"])]


def _period_bounds(q: SourcesQuery, today: date) -> tuple["date | None", "date | None"]:
    if q.custom_range:
        return q.date_from, q.date_to
    if q.period in ("7", "30"):
        return today - timedelta(days=int(q.period) - 1), today
    if q.period == "today":
        return today, today
    if q.period == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    return None, None


def _ranking(rows: list[dict], key: str) -> list[tuple[str, int]]:
    counter = Counter(row[key] for row in rows)
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _top(ranking: list[tuple[str, int]]) -> list[str]:
    return [k for k, _ in ranking[:_TOP_LIMIT]]


def _bucket_key(value: str, top: list[str]) -> str:
    return value if value in top else REST


def _matches(value: str, selected: tuple, top: list[str]) -> bool:
    if not selected:
        return True
    return value in selected or (REST in selected and value not in top)


def _answer_matches(value: str, selected: tuple, top: list[str]) -> bool:
    if not selected:
        return True
    keys = {_match_key(v) for v in selected}
    return _match_key(value) in keys or (REST in selected and value not in top)


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _bucket_label(start: date, step: str, *, short: bool = False) -> str:
    if step == "week":
        end = start + timedelta(days=6)
        sep = "–" if short else " – "
        return f"{start:%d.%m}{sep}{end:%d.%m}"
    if short:
        return f"{start:%d.%m}"
    return f"{start:%d.%m}, {_WEEKDAYS[start.weekday()]}"


def build(conn, scope: queries.Scope, q: SourcesQuery, *, today: "date | None" = None) -> dict:
    today = today or msk_now().date()
    all_rows = _fetch(conn, scope)
    date_from, date_to = _period_bounds(q, today)
    base = [
        r for r in all_rows
        if (date_from is None or r["day"] >= date_from) and (date_to is None or r["day"] <= date_to)
    ]

    rankings = {
        "answer": _ranking(base, "answer"),
        "tag": _ranking(base, "tag"),
        "city": _ranking(base, "city"),
    }
    tops = {k: _top(v) for k, v in rankings.items()}

    filtered = [
        r for r in base
        if (not q.statuses or r["status_key"] in q.statuses)
        and _answer_matches(r["answer"], q.answers, tops["answer"])
        and _matches(r["tag"], q.tags, tops["tag"])
        and (not q.tracks or r["track"] in q.tracks)
        and (not q.ambassador_only or r["amb"])
    ]

    city_labels = {
        row["code"]: row["label"]
        for row in conn.execute("SELECT code, label FROM cities").fetchall()
    }

    # Колонки разбивки: порядок и цвет — по рейтингу в скоупе+периоде ДО фильтров, чтобы
    # цвет категории не прыгал, пока менеджер щёлкает чипы.
    if q.by == "status":
        order = [*STATUS_LABELS, _STATUS_OTHER]
        colors = dict(_STATUS_COLORS)
        labels = {**STATUS_LABELS, _STATUS_OTHER: _STATUS_OTHER_LABEL}
        key_of = lambda r: r["status_key"]  # noqa: E731
    else:
        top = tops[q.by]
        order = [*top, REST]
        colors = {k: str(i + 1) for i, k in enumerate(top)}
        colors[REST] = "other"
        if q.by == "city":
            labels = {k: city_labels.get(k, k) for k in top}
        else:
            labels = {k: k for k in top}
        labels[REST] = REST_LABEL
        field_name = q.by
        key_of = lambda r: _bucket_key(r[field_name], top)  # noqa: E731

    per_cat = Counter(key_of(r) for r in filtered)
    columns = [
        {"key": k, "label": labels[k], "color": colors[k]}
        for k in order if per_cat.get(k)
    ]
    col_index = {c["key"]: i for i, c in enumerate(columns)}

    step = q.step
    bucket_of = _week_start if step == "week" else (lambda d: d)

    # Плотный календарь по данным скоупа+периода (не по отфильтрованным — ось не должна
    # сжиматься от щелчка по чипу): от начала периода/первой заявки до конца периода/сегодня.
    rows_out: list[dict] = []
    if base or date_from is not None:
        start = date_from or min(r["day"] for r in base)
        end = date_to or max([today, *(r["day"] for r in base)])
        if (end - start).days > _MAX_SPAN_DAYS:
            start = end - timedelta(days=_MAX_SPAN_DAYS)
        counts: dict[date, list[int]] = {}
        amb: Counter = Counter()
        for r in filtered:
            b = bucket_of(r["day"])
            counts.setdefault(b, [0] * len(columns))[col_index[key_of(r)]] += 1
            if r["amb"]:
                amb[b] += 1
        cursor = bucket_of(start)
        last = bucket_of(end)
        delta = timedelta(days=7 if step == "week" else 1)
        while cursor <= last:
            c = counts.get(cursor, [0] * len(columns))
            rows_out.append({
                "key": cursor.isoformat(),
                "date": cursor,
                "label": _bucket_label(cursor, step),
                "short_label": _bucket_label(cursor, step, short=True),
                "counts": c,
                "ambassador": amb.get(cursor, 0),
                "total": sum(c),
            })
            cursor += delta
    rows_out.reverse()  # свежий день сверху

    totals_counts = [sum(r["counts"][i] for r in rows_out) for i in range(len(columns))]
    totals = {
        "counts": totals_counts,
        "ambassador": sum(r["ambassador"] for r in rows_out),
        "total": sum(totals_counts),
    }

    # «Все каналы за период»: полный перечень на ключе канала, без хвоста «Остальные».
    # Считаем только заявки внутри отображаемого календаря — иначе после обрезки
    # _MAX_SPAN_DAYS сумма разошлась бы с итогом таблицы.
    day_index = {r["key"]: i for i, r in enumerate(rows_out)}
    matrix = step == "day" and 0 < len(rows_out) <= _MATRIX_MAX_DAYS
    per_channel: dict[str, list[int]] = {}
    for r in filtered:
        idx = day_index.get(bucket_of(r["day"]).isoformat())
        if idx is None:
            continue
        cells = per_channel.setdefault(r["answer"], [0] * len(rows_out))
        cells[idx] += 1
    total_all = totals["total"]
    channels = [
        {
            "label": label,
            "count": sum(cells),
            "share": round(sum(cells) * 100 / total_all, 1) if total_all else 0.0,
            "per_day": cells if matrix else None,
        }
        for label, cells in per_channel.items()
    ]
    channels.sort(key=lambda c: (-c["count"], c["label"]))
    channel_days = (
        [{"key": r["key"], "label": r["short_label"], "date": r["date"]} for r in rows_out]
        if matrix else None
    )

    chronological = list(reversed(rows_out))
    chart = {
        "labels": [r["short_label"] for r in chronological],
        "datasets": [
            {"label": c["label"], "color": c["color"], "data": [r["counts"][i] for r in chronological]}
            for i, c in enumerate(columns)
        ],
        "totals": [r["total"] for r in chronological],
    }

    def _options(kind: str, selected: tuple) -> list[dict]:
        ranking = rankings[kind]
        top = tops[kind]
        norm = _match_key if kind == "answer" else (lambda v: v)
        sel_keys = {norm(v) for v in selected}
        items = [
            {"value": k, "label": k, "count": n, "color": str(i + 1), "tail": False,
             "selected": norm(k) in sel_keys}
            for i, (k, n) in enumerate(ranking[:_TOP_LIMIT])
        ]
        tail = sum(n for _, n in ranking[_TOP_LIMIT:])
        if tail:
            items.append({"value": REST, "label": REST_LABEL, "count": tail, "color": "other",
                          "tail": False, "selected": REST in selected})
        # Лимит 7 — только для цветных колонок: каждое значение хвоста выбирается отдельно.
        for k, n in ranking[_TOP_LIMIT:]:
            items.append({"value": k, "label": k, "count": n, "color": None, "tail": True,
                          "selected": norm(k) in sel_keys})
        listed = {norm(i["value"]) for i in items}
        for value in selected:  # выбранное по старой ссылке, но выпавшее из ranking — видно
            if norm(value) not in listed and value not in top:
                items.append({"value": value, "label": value, "count": None, "color": None,
                              "tail": False, "selected": True})
                listed.add(norm(value))
        return items

    track_counts = Counter(r["track"] for r in base)
    options = {
        "status": [{"value": k, "label": v, "color": _STATUS_COLORS[k]} for k, v in STATUS_LABELS.items()],
        "answer": _options("answer", q.answers),
        "tag": _options("tag", q.tags),
        "track": [
            {"value": k, "label": TRACK_LABELS.get(k, k), "count": n}
            for k, n in sorted(track_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }

    return {
        "has_data": totals["total"] > 0,
        "base_count": len(base),
        "columns": columns,
        "rows": rows_out,
        "totals": totals,
        "chart": chart,
        "channels": channels,
        "channel_days": channel_days,
        "options": options,
        "city_labels": city_labels,
        "range": (date_from, date_to),
        "step": step,
        "by": q.by,
    }


# ── подписи и CSV ────────────────────────────────────────────────────────────────────────

def describe(q: SourcesQuery, *, city_label: "str | None", track_labels=None) -> str:
    """Человеческая сводка выбранного среза — над таблицей и первой строкой CSV, чтобы
    выгрузка не теряла контекст («это только СПб и только одобренные»)."""
    parts = [f"Разбивка: {BY_LABELS[q.by].lower()}", f"шаг: {'неделя' if q.step == 'week' else 'день'}"]
    if q.custom_range:
        start = q.date_from.strftime("%d.%m.%Y") if q.date_from else "начала"
        end = q.date_to.strftime("%d.%m.%Y") if q.date_to else "сегодня"
        parts.append(f"период: с {start} по {end}")
    else:
        parts.append(f"период: {PERIOD_LABELS[q.period].lower()}")
    parts.append(f"город: {city_label or 'все города'}")
    filters: list[str] = []
    if q.statuses:
        filters.append("статус — " + ", ".join(STATUS_LABELS[s].lower() for s in q.statuses))
    if q.answers:
        filters.append("канал — " + ", ".join(REST_LABEL if v == REST else v for v in q.answers))
    if q.tags:
        filters.append("ссылка — " + ", ".join(REST_LABEL if v == REST else v for v in q.tags))
    if q.tracks:
        labels = track_labels or TRACK_LABELS
        filters.append("трек — " + ", ".join(labels.get(v, v) for v in q.tracks))
    if q.ambassador_only:
        filters.append("только по ссылке амбассадора")
    parts.append("фильтры: " + ("; ".join(filters) if filters else "нет"))
    return " · ".join(parts)


def _csv_period_cell(row: dict, step: str) -> str:
    start: date = row["date"]
    if step == "week":
        return f"{start:%d.%m.%Y} – {start + timedelta(days=6):%d.%m.%Y}"
    return f"{start:%d.%m.%Y}"


def csv_bytes(result: dict, description: str) -> bytes:
    """CSV текущего среза: «;», UTF-8 с BOM (Excel открывает кириллицу без мастера импорта) —
    тот же формат, что у выгрузок бота (`arrival_stats.csv_bytes`). Подписи категорий
    вводят люди — через `_sheet_safe`, чтобы Excel не принял их за формулу. После «Итого» —
    отдельная секция «Все каналы за период» (матрица канал × день или итог и доля)."""
    week = result["step"] == "week"
    out = io.StringIO()
    raw = csv.writer(out, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")

    def w(row):
        raw.writerow([_sheet_safe(v) if isinstance(v, str) else v for v in row])

    w(["Источники по неделям" if week else "Источники по дням"])
    w([description])
    w([])
    w([
        "Неделя" if week else "День",
        *[c["label"] for c in result["columns"]],
        "По ссылке амбассадора",
        "Всего за неделю" if week else "Всего за день",
    ])
    for row in result["rows"]:
        w([_csv_period_cell(row, result["step"]), *row["counts"], row["ambassador"], row["total"]])
    totals = result["totals"]
    w(["Итого", *totals["counts"], totals["ambassador"], totals["total"]])
    w([])
    w(["Все каналы за период"])
    days = result.get("channel_days")

    def share(c):
        return f"{c['share']:.1f}".replace(".", ",")

    if days:
        w(["Канал", *[d["date"].strftime("%d.%m.%Y") for d in days], "Итого", "Доля, %"])
        for c in result["channels"]:
            w([c["label"], *c["per_day"], c["count"], share(c)])
    else:
        w(["Канал", "Заявок", "Доля, %"])
        for c in result["channels"]:
            w([c["label"], c["count"], share(c)])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def csv_data_href(result: dict, description: str) -> str:
    """Выгрузка отдаётся ссылкой `data:` прямо со страницы, а не отдельным маршрутом:
    у дашборда маршрутов выгрузки нет по решению D-17 (сторож
    test_no_app_route_and_no_export_or_csv_route), а здесь только агрегаты, которые и так
    видны на странице — доступ к файлу ровно тот же, что к самой странице."""
    return "data:text/csv;charset=utf-8," + urllib.parse.quote(csv_bytes(result, description), safe="")
