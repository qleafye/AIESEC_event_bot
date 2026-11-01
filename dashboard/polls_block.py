"""Раздел «Опросы» страницы «🎪 Статистика форума» — итоги опросов и анкет без ПД.

Отдельный модуль, а не ещё одна функция в `dashboard/queries.py` (тот уже около 3000 строк).
Дашборд — отдельный контейнер, копирует только каталог `dashboard/` (и `shared/`): ни
`database/`, ни `services/` ему недоступны, поэтому SQL и подсчёт здесь повторяют бота:
обычный опрос — `database.db.get_poll_results` / `count_poll_deliveries` («ответили N из
получивших M»), анкета — `services.comms.survey_report.build_survey_results`. Паритет сверяет
`tests/test_dashboard_polls_261028.py`; при правке подсчёта в боте — править и здесь.

Без персональных данных: наружу уходят только числа и тексты свободных ответов — ни имён,
ни ников, ни id, ни городов людей, ни времени ответа. Тексты экранирует шаблон (Jinja).

Скоуп: у опросов нет сезона — блок считается только для текущего сезона (для прошлого —
`{"past_season": True}`). Город сужает и сами опросы (адресованные этому городу или всем),
и людей — получателей и ответивших — по `users.event_city`.
"""
from __future__ import annotations

import json

from dashboard.queries import Scope, _city_fragment, _current_event_season

_POLL_LIMIT = 10
_TEXTS_LIMIT = 5
_TEXT_MAX = 300
_STATUSES = ("open", "sending", "closed")
# Копия `database.db.POLL_STATUS_LABELS` для показываемых статусов — дрейф ловит тест.
_STATUS_LABELS = {
    "sending": "⏳ Отправляется",
    "open": "🟢 Открыт",
    "closed": "⏹ Закрыт",
}
_KIND_LABELS = {"poll": "Опрос", "survey": "Анкета"}


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _loads(raw, default):
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _recipients_sql(conn, scope: Scope, column: str) -> tuple[str, list]:
    """` AND <column> IN (людей города)` или пусто без города."""
    if scope.city is None:
        return "", []
    frag, params = _city_fragment(conn, scope.city)
    return f" AND {column} IN (SELECT telegram_id FROM users WHERE {frag})", list(params)


def _pct(n: int, total: int) -> int:
    return round(100 * n / total) if total else 0


def _fmt_avg(avg) -> str:
    return f"{avg:.1f}".replace(".", ",") if avg is not None else "—"


def _fmt_date(raw) -> str:
    raw = str(raw or "")
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw[8:10]}.{raw[5:7]}.{raw[0:4]}"
    return ""


def _short(text: str) -> str:
    return text if len(text) <= _TEXT_MAX else text[: _TEXT_MAX - 1] + "…"


def _opt_text(options: list, i: int) -> str:
    if 0 <= i < len(options):
        o = options[i]
        return str(o.get("t", "") if isinstance(o, dict) else o)
    return ""


def _options(labels: list[str], counts: list[int]) -> list[dict]:
    total = sum(counts)
    return [{"label": label, "count": n, "pct": _pct(n, total)} for label, n in zip(labels, counts)]


def _sent(conn, pid: int, scope: Scope) -> int:
    rsql, rparams = _recipients_sql(conn, scope, "chat_id")
    row = conn.execute(
        f"SELECT COUNT(*) FROM poll_messages WHERE poll_id = ? AND status = 'ok'{rsql}",
        [pid, *rparams],
    ).fetchone()
    return int(row[0] or 0)


def _poll_item(conn, poll, scope: Scope) -> dict:
    """Обычный опрос: неанонимный — по `poll_answers`, анонимный — суммой `totals_json`."""
    pid = poll["id"]
    labels = [str(o) for o in _loads(poll["options_json"], [])]
    counts = [0] * len(labels)
    anonymous = bool(poll["is_anonymous"])
    if anonymous:
        rsql, rparams = _recipients_sql(conn, scope, "chat_id")
        answered = 0
        rows = conn.execute(
            "SELECT totals_json FROM poll_messages WHERE poll_id = ? AND status = 'ok' "
            f"AND message_id IS NOT NULL{rsql}",
            [pid, *rparams],
        ).fetchall()
        for (raw,) in rows:
            totals = _loads(raw, None)
            if not isinstance(totals, dict):
                continue
            answered += int(totals.get("total") or 0)
            for i, n in enumerate((totals.get("options") or [])[: len(counts)]):
                counts[i] += int(n or 0)
    else:
        rsql, rparams = _recipients_sql(conn, scope, "user_id")
        rows = conn.execute(
            f"SELECT option_ids_json FROM poll_answers WHERE poll_id = ?{rsql}", [pid, *rparams]
        ).fetchall()
        answered = len(rows)
        for (raw,) in rows:
            for i in _loads(raw, []):
                if isinstance(i, int) and 0 <= i < len(counts):
                    counts[i] += 1
    return {
        "anonymous": anonymous,
        "answered": answered,
        "started_not_done": 0,
        "options": _options(labels, counts),
        "questions": [],
    }


def _is_text_answer(qtype: str, sub: str, value) -> bool:
    if not isinstance(value, str):
        return False
    if qtype == "text":
        return sub == ""
    return sub.startswith("m")


def _free_texts(question: dict, answers: list[dict]) -> tuple[list[dict], int]:
    """Последние `_TEXTS_LIMIT` свободных ответов/уточнений (`answers` уже по убыванию времени)
    и их общее число. Только текст и подпись варианта — без людей и времени."""
    options = question["options"]
    picked = [a for a in answers if _is_text_answer(question["qtype"], a["sub"], a["value"])]
    out = []
    for a in picked[:_TEXTS_LIMIT]:
        note = ""
        if a["sub"].startswith("m"):
            idx = a["sub"][1:]
            label = _opt_text(options, int(idx)) if idx.isdigit() else ""
            note = f"уточнение к «{label}»" if label else "уточнение"
        out.append({"text": _short(a["value"]), "note": note})
    return out, len(picked)


def _survey_question_item(question: dict, answers: list[dict]) -> dict:
    """Числа одного вопроса — как `survey_report.build_survey_results`."""
    qt = question["qtype"]
    item: dict = {"text": question["text"], "type": qt}
    if qt in ("single", "multi"):
        labels = [_opt_text(question["options"], i) for i in range(len(question["options"]))]
        counts = [0] * len(labels)
        people = set()
        for a in answers:
            if a["sub"] != "":
                continue
            v = a["value"]
            for i in (v if isinstance(v, list) else [v]):
                if isinstance(i, int) and 0 <= i < len(counts):
                    counts[i] += 1
                    people.add(a["user_id"])
        item.update(options=_options(labels, counts), respondents=len(people))
    elif qt == "scale":
        dist = [0] * 5
        for a in answers:
            v = a["value"]
            if a["sub"] == "" and isinstance(v, int) and 1 <= v <= 5:
                dist[v - 1] += 1
        n = sum(dist)
        avg = sum((i + 1) * c for i, c in enumerate(dist)) / n if n else None
        item.update(dist=_options([str(i) for i in range(1, 6)], dist), avg_label=_fmt_avg(avg),
                    respondents=n)
    elif qt == "table":
        rows = []
        for k, name in enumerate(question["rows"]):
            vals = [a["value"] for a in answers
                    if a["sub"] == f"r{k}" and isinstance(a["value"], int) and 1 <= a["value"] <= 5]
            rows.append({"label": str(name), "n": len(vals),
                         "avg_label": _fmt_avg(sum(vals) / len(vals) if vals else None)})
        item.update(rows=rows, respondents=len({a["user_id"] for a in answers}))
    else:
        item.update(respondents=len({a["user_id"] for a in answers if a["sub"] == ""}))
    texts, total = _free_texts(question, answers)
    item.update(texts=texts, total_texts=total)
    return item


def _survey_item(conn, poll, scope: Scope) -> dict:
    pid = poll["id"]
    if not (_has_table(conn, "survey_questions") and _has_table(conn, "survey_answers")
            and _has_table(conn, "survey_progress")):
        return {"anonymous": False, "answered": 0, "started_not_done": 0, "options": [],
                "questions": []}
    rsql, rparams = _recipients_sql(conn, scope, "user_id")
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN state != 'done' THEN 1 ELSE 0 END), 0) "
        f"FROM survey_progress WHERE poll_id = ?{rsql}",
        [pid, *rparams],
    ).fetchone()
    answered, not_done = int(row[0] or 0), int(row[1] or 0)

    grouped: dict[int, list[dict]] = {}
    for a in conn.execute(
        "SELECT user_id, question_id, sub, value_json FROM survey_answers "
        f"WHERE poll_id = ?{rsql} ORDER BY answered_at DESC, rowid DESC",
        [pid, *rparams],
    ).fetchall():
        grouped.setdefault(a["question_id"], []).append({
            "user_id": a["user_id"], "sub": a["sub"] or "", "value": _loads(a["value_json"], None),
        })
    questions = []
    for q in conn.execute(
        "SELECT id, qtype, text, options_json, rows_json FROM survey_questions "
        "WHERE poll_id = ? ORDER BY position, id",
        (pid,),
    ).fetchall():
        question = {"qtype": q["qtype"], "text": q["text"],
                    "options": _loads(q["options_json"], []), "rows": _loads(q["rows_json"], [])}
        questions.append(_survey_question_item(question, grouped.get(q["id"], [])))
    return {"anonymous": False, "answered": answered, "started_not_done": not_done,
            "options": [], "questions": questions}


def polls_block(conn, scope: Scope) -> "dict | None":
    """`None` — нет ни одного разосланного опроса/анкеты с получателями в скоупе.
    `{"past_season": True}` — выбран прошлый сезон (опросы к сезону не привязаны).
    Иначе `{"items": [...], "total": N, "shown": n}`, новые сверху, не больше `_POLL_LIMIT`."""
    if not (_has_table(conn, "polls") and _has_table(conn, "poll_messages")
            and _has_table(conn, "poll_answers")):
        return None
    if scope.season is not None and scope.season != _current_event_season(conn):
        return {"past_season": True}

    kind_col = "kind" if _has_column(conn, "polls", "kind") else "'poll'"
    where = [f"p.status IN ({', '.join('?' for _ in _STATUSES)})"]
    params: list = list(_STATUSES)
    if scope.city is not None:
        where.append("(p.city IS NULL OR p.city = ?)")
        params.append(scope.city)
    rsql, rparams = _recipients_sql(conn, scope, "m.chat_id")
    where.append(
        "EXISTS (SELECT 1 FROM poll_messages m WHERE m.poll_id = p.id AND m.status = 'ok'"
        f"{rsql})"
    )
    params.extend(rparams)
    polls = conn.execute(
        "SELECT p.id, p.question, p.options_json, p.is_anonymous, p.status, p.created_at, "
        f"{kind_col} AS kind FROM polls p WHERE {' AND '.join(where)} ORDER BY p.id DESC",
        params,
    ).fetchall()
    if not polls:
        return None

    items = []
    for poll in polls[:_POLL_LIMIT]:
        kind = poll["kind"] if poll["kind"] in _KIND_LABELS else "poll"
        body = _survey_item(conn, poll, scope) if kind == "survey" else _poll_item(conn, poll, scope)
        items.append({
            "title": poll["question"],
            "kind_label": _KIND_LABELS[kind],
            "status_label": _STATUS_LABELS.get(poll["status"], poll["status"]),
            "created": _fmt_date(poll["created_at"]),
            "sent": _sent(conn, poll["id"], scope),
            **body,
        })
    return {"items": items, "total": len(polls), "shown": len(items)}
