"""Общий сервис поиска человека — по @username, числовому telegram id или части ФИО.

Основной источник — таблица `users` (строка появляется только на ПОДАЧУ анкеты,
users_row_only_on_submit). Дополнительно ищет в `reg_started` (кто нажал /start, но анкету
не подал) — ТОЛЬКО если человека нет в `users`: это фоллбэк, не второй равноправный
источник. Единый формат результата нужен и текущей выдаче ролей (`handlers/admin_roles.py`,
починка поиска по @username), и будущим потребителям — сканеру отметки на форуме (поиск по
фамилии, когда у делегата сел телефон; город/вуз в выдаче различают тёзок) и карточке
делегата `/find` (по @username, id или имени).
"""
from __future__ import annotations

import json
import re

from database import db

# Юзернейм Telegram: латиница/цифры/подчёркивание, 5-32 символа, начинается с буквы.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TME_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/?$", re.IGNORECASE)


def parse_query(raw: str) -> tuple[str, str | int]:
    """Разбирает пользовательский ввод на (kind, value):
    - "username" — из `@name`, `t.me/name`/`https://t.me/name` или голого юзернейма
      (латиница/цифры/подчёркивание, 5-32 символа) — value остаётся строкой БЕЗ «@»;
    - "id" — из голого числа (может быть отрицательным — телеграм id каналов) — value: int;
    - "name" — всё остальное (в том числе кириллица, пробелы, пустая строка) — часть ФИО.
    Пустой ввод -> ("name", "") — вызывающий обязан вернуть «не найдено», не делая запроса."""
    q = (raw or "").strip()
    if not q:
        return ("name", "")
    m = _TME_RE.match(q)
    if m:
        return ("username", m.group(1))
    if q.startswith("@"):
        return ("username", q)
    if q.isascii() and q.lstrip("-").isdigit() and q.lstrip("-"):
        return ("id", int(q))
    if _USERNAME_RE.fullmatch(q):
        return ("username", q)
    return ("name", q)


def _city_matches(event_city: str | None, city_scope) -> bool:
    """Питоновское зеркало `database.db._city_clause` — для точечных находок (по username/id),
    где строка уже получена одним запросом и город фильтруется постфактум, без второго похода
    в базу. `city_scope=None` -> пропускает всё (как и `_city_clause`)."""
    if city_scope is None:
        return True
    code, exclude = city_scope
    if not exclude:
        return event_city == code
    return event_city is None or event_city not in exclude


def _normalize_name(s: str | None) -> str:
    """Тот же ё/е-фолд, что `database.db._normalize_search_text` (там же обоснование): на
    стойке форума фамилию чаще всего набирают без «ё». `reg_started.partial_data` — сырой
    JSON, не SQL-колонка, поэтому сравнение идёт в Python, а не через ту же SQL-функцию."""
    return (s or "").lower().replace("ё", "е")


def _parse_partial(partial_json: str | None) -> dict:
    """Тот же паттерн деградации, что `handlers.registration.incomplete_sheet_row`:
    отсутствующий/битый JSON -> пустой словарь, не исключение."""
    if not partial_json:
        return {}
    try:
        parsed = json.loads(partial_json)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _from_users_row(row: dict) -> dict:
    return {
        "user_id": row["telegram_id"],
        "full_name": row.get("full_name") or None,
        "username": row.get("username") or None,
        "city": row.get("event_city") or None,
        "university": row.get("university") or None,
        "status": row.get("status") or None,
        "source": "users",
    }


def _from_reg_started_row(row: dict, *, partial: dict | None = None) -> dict:
    partial = partial if partial is not None else _parse_partial(row.get("partial_data"))
    full_name = partial.get("full_name") or None
    return {
        "user_id": row["telegram_id"],
        "full_name": full_name,
        "username": row.get("username") or None,
        "city": row.get("event_city") or None,
        "university": partial.get("university") or None,
        "status": None,
        "source": "reg_started",
    }


async def search_people(
    query: str, *, city_scope=None, limit: int = 20, include_started: bool = True,
) -> list[dict]:
    """Единая точка поиска. Возвращает список словарей одного формата — user_id, full_name,
    username, city, university, status, source ('users' | 'reg_started'). `source` различает
    завершивших анкету от нажавших /start, но не подавших её (последние появляются в выдаче,
    только если в `users` их нет). `city_scope` — дескриптор `cities.city_scope(...)`:
    привязанный к городу менеджер видит только своих (как в `search_users_by_name`).
    `include_started=False` выключает reg_started целиком — там, где нужны только
    полноценно зарегистрированные."""
    kind, value = parse_query(query)

    if kind == "username":
        user = await db.get_user_by_username(value)
        if user is not None:
            if not _city_matches(user.get("event_city"), city_scope):
                return []
            return [_from_users_row(user)]
        if not include_started:
            return []
        started = await db.get_reg_started_by_username(value)
        if started is None or not _city_matches(started.get("event_city"), city_scope):
            return []
        return [_from_reg_started_row(started)]

    if kind == "id":
        user = await db.get_user(value)
        if user is not None:
            if not _city_matches(user.get("event_city"), city_scope):
                return []
            return [_from_users_row(user)]
        if not include_started:
            return []
        started = await db.get_reg_started_by_id(value)
        if started is None or not _city_matches(started.get("event_city"), city_scope):
            return []
        return [_from_reg_started_row(started)]

    # kind == "name": часть ФИО, возможны тёзки — различимы по city/university в выдаче.
    if not value:
        return []
    results = [
        _from_users_row(row)
        for row in await db.search_users_by_name(
            value, limit, city_scope=city_scope, include_university=True,
        )
    ]
    if include_started and len(results) < limit:
        seen_ids = {r["user_id"] for r in results}
        needle = _normalize_name(value)
        for tid, username, _started_at, _last_step, partial_json, event_city in (
            await db.get_incomplete_rows_with_city()
        ):
            if tid in seen_ids:
                continue
            if not _city_matches(event_city, city_scope):
                continue
            partial = _parse_partial(partial_json)
            full_name = partial.get("full_name") or ""
            if needle not in _normalize_name(full_name):
                continue
            results.append(_from_reg_started_row(
                {"telegram_id": tid, "username": username, "event_city": event_city},
                partial=partial,
            ))
            if len(results) >= limit:
                break
    return results[:limit]
