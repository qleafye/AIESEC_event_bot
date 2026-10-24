"""Итоги анкеты для менеджера и выгрузка в таблицу.

Итоги по вопросам считаются из `survey_answers`, режутся на страницы ≤ 4096 символов по границе
вопроса; свободные ответы и уточнения — отдельный экран по 10 штук. Выгрузка — одна вкладка
«Опрос <id>» на анкету: строка на делегата, колонка на вопрос (у таблицы — на каждую строку,
у варианта с уточнением — своя колонка). Общая вкладка «Опросы» анкет не касается.
"""
from __future__ import annotations

import html as html_module
import logging

from database.survey_db import (
    get_survey_question,
    list_survey_answers,
    list_survey_progress,
    list_survey_questions,
)
from services.comms.polls import _bar, _fmt_date, _sheet_safe
from services.comms.surveys import DEFAULT_NA_LABEL, TG_MESSAGE_MAX

logger = logging.getLogger(__name__)

PAGE_LIMIT = TG_MESSAGE_MAX - 200  # запас под заголовок и служебные строки
TEXT_PAGE_SIZE = 10
SHEET_HEADERS_HEAD = ["Дата", "Telegram ID", "ФИО", "@ник", "Город", "Статус"]
_STATUS_DONE = "завершил"
_STATUS_PART = "не закончил"


def _esc(value) -> str:
    return html_module.escape(str(value if value is not None else ""))


def _opt_text(q: dict, i: int) -> str:
    opts = q.get("options") or []
    if 0 <= i < len(opts):
        o = opts[i]
        return str(o.get("t", "") if isinstance(o, dict) else o)
    return ""


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _is_text_answer(q: dict, a: dict) -> bool:
    if not isinstance(a.get("value"), str):
        return False
    if q["qtype"] == "text":
        return a["sub"] == ""
    return a["sub"].startswith("m")


async def build_survey_results(poll_id: int) -> list[dict]:
    """Список итогов по вопросам в порядке анкеты."""
    questions = await list_survey_questions(poll_id)
    grouped: dict[int, list[dict]] = {}
    for a in await list_survey_answers(poll_id):
        grouped.setdefault(a["question_id"], []).append(a)
    out = []
    for q in questions:
        rows = grouped.get(q["id"], [])
        qt = q["qtype"]
        item: dict = {"q": q, "type": qt}
        if qt in ("single", "multi"):
            counts = [0] * len(q["options"])
            people = set()
            for a in rows:
                if a["sub"] != "":
                    continue
                v = a["value"]
                for i in (v if isinstance(v, list) else [v]):
                    if isinstance(i, int) and 0 <= i < len(counts):
                        counts[i] += 1
                        people.add(a["user_id"])
            item.update(counts=counts, respondents=len(people))
        elif qt == "scale":
            dist = [0] * 5
            for a in rows:
                v = a["value"]
                if a["sub"] == "" and isinstance(v, int) and 1 <= v <= 5:
                    dist[v - 1] += 1
            n = sum(dist)
            avg = sum((i + 1) * c for i, c in enumerate(dist)) / n if n else None
            item.update(dist=dist, respondents=n, avg=avg)
        elif qt == "table":
            per_row = []
            for k, name in enumerate(q["rows"]):
                vals = [a["value"] for a in rows
                        if a["sub"] == f"r{k}" and isinstance(a["value"], int) and 1 <= a["value"] <= 5]
                per_row.append({"name": name, "n": len(vals),
                                "avg": sum(vals) / len(vals) if vals else None})
            item.update(rows=per_row, respondents=len({a["user_id"] for a in rows}))
        else:
            item.update(respondents=len({a["user_id"] for a in rows if a["sub"] == ""}))
        item["texts"] = sum(1 for a in rows if _is_text_answer(q, a))
        out.append(item)
    return out


def _fmt_avg(avg) -> str:
    return f"{avg:.1f}".replace(".", ",") if avg is not None else "—"


def _render_item(k: int, item: dict) -> str:
    q = item["q"]
    lines = [f"<b>{k}. {_esc(q['text'])}</b>"]
    qt = item["type"]
    if qt in ("single", "multi"):
        counts = item["counts"]
        total = sum(counts)
        for i, n in enumerate(counts):
            pct = round(100 * n / total) if total else 0
            lines.append(f"{_bar(n, total)} {n} ({pct}%) — {_esc(_opt_text(q, i))}")
        lines.append(f"Ответили: {item['respondents']}")
    elif qt == "scale":
        dist = item["dist"]
        total = sum(dist)
        for i, n in enumerate(dist, start=1):
            pct = round(100 * n / total) if total else 0
            lines.append(f"{_bar(n, total)} {i}: {n} ({pct}%)")
        lines.append(f"Среднее: {_fmt_avg(item['avg'])} · оценок: {total}")
    elif qt == "table":
        for r in item["rows"]:
            lines.append(f"{_esc(r['name'])} — среднее {_fmt_avg(r['avg'])} (оценок: {r['n']})")
    else:
        lines.append(f"Ответили: {item['respondents']}")
    if item.get("texts") and qt != "text":
        lines.append(f"💬 Уточнений: {item['texts']}")
    return "\n".join(lines)


def render_results_pages(poll: dict, results: list[dict], started: int, finished: int) -> list[str]:
    """Страницы итогов; каждая ≤ 4096. Первая начинается со сводки «Начали / Закончили»."""
    header = (f"📊 <b>{_esc(poll['question'])}</b>\n"
              f"Начали: {started} · Закончили: {finished}")
    blocks = [_render_item(k, it) for k, it in enumerate(results, start=1)]
    blocks = [b if len(b) <= PAGE_LIMIT else b[: PAGE_LIMIT - 1] + "…" for b in blocks]
    pages: list[str] = []
    cur = header
    for b in blocks:
        if len(cur) + 2 + len(b) > PAGE_LIMIT:
            pages.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}"
    pages.append(cur)
    return pages


async def render_text_answers_page(poll_id: int, qid: int, page: int) -> tuple[str, int]:
    """(текст, число страниц). Ответы вопроса: свободные и уточнения, по 10 на страницу."""
    q = await get_survey_question(qid)
    if q is None or q["poll_id"] != poll_id:
        return "Вопрос не найден.", 1
    items = []
    for a in await list_survey_answers(poll_id):
        if a["question_id"] != qid or not _is_text_answer(q, a):
            continue
        label = ""
        if a["sub"].startswith("m") and a["sub"][1:].isdigit():
            label = f" [{_esc(_opt_text(q, int(a['sub'][1:])))}]"
        items.append(f"• {_esc(a['value'])}{label}")
    pages = max(1, -(-len(items) // TEXT_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * TEXT_PAGE_SIZE:(page + 1) * TEXT_PAGE_SIZE]
    head = f"💬 <b>{_esc(_short(q['text'], 120))}</b>\nОтветов: {len(items)} · стр. {page + 1}/{pages}\n"
    body = "\n".join(chunk) if chunk else "Пока никто не ответил."
    text = f"{head}\n{body}"
    if len(text) > TG_MESSAGE_MAX:
        text = text[: TG_MESSAGE_MAX - 1] + "…"
    return text, pages


def _columns(questions: list[dict]) -> list[tuple[str, int, str, str]]:
    """(заголовок, question_id, sub, kind) — колонки вопросов выгрузки."""
    cols = []
    for k, q in enumerate(questions, start=1):
        base = f"{k}. {_short(q['text'], 40)}"
        if q["qtype"] == "table":
            for r, name in enumerate(q["rows"]):
                cols.append((f"{base} — {name}", q["id"], f"r{r}", "table"))
        else:
            cols.append((base, q["id"], "", q["qtype"]))
            if q["qtype"] in ("single", "multi"):
                for i, o in enumerate(q["options"]):
                    if isinstance(o, dict) and (o.get("more") or "").strip():
                        cols.append((f"{base} — уточнение: {o.get('t', '')}", q["id"], f"m{i}", "more"))
    return cols


def _cell(q: dict, kind: str, value) -> str:
    if value is None:
        return ""
    if kind in ("single", "multi"):
        picked = value if isinstance(value, list) else [value]
        return "; ".join(_opt_text(q, i) for i in picked if isinstance(i, int))
    if kind == "table" and value == 0:
        return q.get("na_label") or DEFAULT_NA_LABEL
    return str(value)


async def build_survey_sheet(poll_id: int) -> tuple[list[str], list[list]]:
    """(заголовки, строки): строка на делегата, незаконченные тоже."""
    from domain.cities import city_label  # cities импортирует database.db
    questions = await list_survey_questions(poll_id)
    qmap = {q["id"]: q for q in questions}
    cols = _columns(questions)
    answers = {(a["user_id"], a["question_id"], a["sub"]): a["value"]
               for a in await list_survey_answers(poll_id)}
    headers = SHEET_HEADERS_HEAD + [c[0] for c in cols]
    rows = []
    for p in await list_survey_progress(poll_id):
        city = await city_label(p["event_city"]) if p.get("event_city") else ""
        row = [
            _fmt_date(p.get("finished_at") or p.get("started_at")),
            p["user_id"], p.get("full_name") or "", p.get("username") or "", city,
            _STATUS_DONE if p.get("state") == "done" else _STATUS_PART,
        ]
        for _title, qid, sub, kind in cols:
            row.append(_cell(qmap[qid], kind, answers.get((p["user_id"], qid, sub))))
        rows.append([_sheet_safe(v) for v in row])
    return [_sheet_safe(h) for h in headers], rows


async def export_survey_to_sheet(poll_id: int) -> int:
    """Полная перезапись вкладки «Опрос <id>». Число строк или -1, если таблица недоступна.
    Название анкеты в имя вкладки не входит: переименование листа создало бы новую вкладку."""
    from services.sheets.sheets import sync_named_worksheet  # тянет gspread — лениво
    try:
        headers, rows = await build_survey_sheet(poll_id)
        return await sync_named_worksheet(f"Опрос {poll_id}", headers, rows)
    except Exception as e:
        logger.error("export_survey_to_sheet(%s) failed: %s", poll_id, e)
        return -1
