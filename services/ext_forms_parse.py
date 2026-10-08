"""Чистый разбор ответов и вопросов Яндекс Форм: без сети и БД.

Ответ API -> список {"q", "label", "value"}: ключ вопроса — data[].id, значение сплющено в текст
(строка как есть, список через «, », файл — имя файла, число/bool — str).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from services.timeutil import MOSCOW_TZ

_FMT = "%Y-%m-%d %H:%M:%S"


def _flat(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(_flat(v) for v in value)
    if isinstance(value, dict):
        for key in ("name", "text", "label", "value"):
            inner = value.get(key)
            if isinstance(inner, (str, int, float)) and not isinstance(inner, bool):
                return str(inner)
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _to_msk(created) -> str | None:
    if not isinstance(created, str) or not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_TZ).strftime(_FMT)


def parse_yandex_answer(raw: dict) -> tuple[str | None, list[dict]]:
    items: list[dict] = []
    for entry in (raw or {}).get("data") or []:
        if not isinstance(entry, dict):
            continue
        items.append({
            "q": str(entry.get("id")),
            "label": str(entry.get("label") or ""),
            "value": _flat(entry.get("value")),
        })
    return _to_msk((raw or {}).get("created")), items


_PUSH_ANSWER_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}$")


def _push_answer_id(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if _PUSH_ANSWER_ID_RE.match(text) else None


def _push_items(answers) -> list[dict] | None:
    if isinstance(answers, str):
        try:
            answers = json.loads(answers)
        except ValueError:
            return None
    items: list[dict] = []
    if isinstance(answers, dict):
        if isinstance(answers.get("data"), list):
            return parse_yandex_answer(answers)[1] or None
        for key, value in answers.items():
            items.append({"q": str(key), "label": str(key), "value": _flat(value)})
    elif isinstance(answers, list):
        for entry in answers:
            if not isinstance(entry, dict):
                continue
            q = next((entry[k] for k in ("id", "key", "name") if entry.get(k) is not None), None)
            label = next((entry[k] for k in ("label", "question", "text") if entry.get(k)), None)
            if q is None:
                q = label
            if q is None:
                continue
            value = entry.get("value", entry.get("answer"))
            items.append({"q": str(q), "label": str(label if label is not None else q),
                          "value": _flat(value)})
    return items or None


def parse_push_body(body, *, header_answer_id: str | None = None) -> dict:
    """Тело запроса интеграции личной формы -> answer_id / form_id / created (МСК) / items.
    Терпимый разбор: JSON-RPC params или плоский JSON; на любом входе не бросает исключений."""
    result = {"answer_id": None, "form_id": None, "created": None, "items": None}
    try:
        params = body
        if isinstance(body, dict) and isinstance(body.get("params"), dict):
            params = body["params"]
        if not isinstance(params, dict):
            result["answer_id"] = _push_answer_id(header_answer_id)
            return result
        result["answer_id"] = _push_answer_id(params.get("answer_id")) or _push_answer_id(
            header_answer_id)
        fid = params.get("form_id")
        if isinstance(fid, (str, int)) and not isinstance(fid, bool) and str(fid).strip():
            result["form_id"] = str(fid).strip()
        result["created"] = _to_msk(params.get("created"))
        result["items"] = _push_items(params.get("answers"))
    except Exception:  # noqa: BLE001 — разбор чужого тела не должен ронять приёмник
        pass
    return result


def parse_yandex_questions(raw: dict) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for page in (raw or {}).get("pages") or []:
        for q in (page or {}).get("items") or []:
            if isinstance(q, dict) and q.get("id") is not None:
                result.append((str(q["id"]), str(q.get("label") or "")))
    return result
