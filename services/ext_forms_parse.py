"""Чистый разбор ответов и вопросов Яндекс Форм: без сети и БД.

Ответ API -> список {"q", "label", "value"}: ключ вопроса — data[].id, значение сплющено в текст
(строка как есть, список через «, », файл — имя файла, число/bool — str).
"""
from __future__ import annotations

import json
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


def parse_yandex_questions(raw: dict) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for page in (raw or {}).get("pages") or []:
        for q in (page or {}).get("items") or []:
            if isinstance(q, dict) and q.get("id") is not None:
                result.append((str(q["id"]), str(q.get("label") or "")))
    return result
