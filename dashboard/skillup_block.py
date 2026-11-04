"""Срезы дашборда: опыт в IT от трёх лет и желание в кейс.

На страницу выходят только числа. Реестр бота этот модуль не читает:
выключенный тумблер и отсутствующая колонка дают None.
"""
from __future__ import annotations

import sqlite3

_EMPTY = "не указано"


def _columns(conn) -> set[str]:
    try:
        rows = conn.execute("PRAGMA table_info(users)").fetchall()
    except sqlite3.Error:
        return set()
    return {row[1] for row in rows}


def _enabled(conn, key: str) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == "on"


def _percent(count: int, total: int):
    if not total:
        return None
    return int(round(count * 100 / total))


def _scope_where(conn, scope):
    from dashboard.queries import _scope_sql

    parts, params = _scope_sql(conn, scope)
    if not parts:
        return "", ()
    return " WHERE " + " AND ".join(parts), params


def _bars(counts: dict[str, int], base: int) -> list[dict]:
    rows = [
        {"label": label, "count": count, "pct": _percent(count, base) or 0}
        for label, count in counts.items()
    ]
    rows.sort(key=lambda row: (-row["count"], row["label"]))
    return rows


def it_block(conn, scope) -> dict | None:
    if not _enabled(conn, "dashboard_block_it3plus"):
        return None
    if "is_it_3plus" not in _columns(conn):
        return None
    where, params = _scope_where(conn, scope)
    try:
        row = conn.execute(
            "SELECT SUM(CASE WHEN is_it_3plus = 1 THEN 1 ELSE 0 END), COUNT(*) "
            f"FROM users{where}",
            params,
        ).fetchone()
    except sqlite3.Error:
        return None
    count = int(row[0] or 0)
    total = int(row[1] or 0)
    return {"count": count, "total": total, "percent": _percent(count, total)}


def _plain(value) -> str:
    text = str(value or "").strip()
    return text or _EMPTY


def _tokens(value) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def case_block(conn, scope) -> dict | None:
    if not _enabled(conn, "dashboard_block_case_optin"):
        return None
    if "case_optin" not in _columns(conn):
        return None
    where, params = _scope_where(conn, scope)
    try:
        rows = conn.execute(
            "SELECT study_field, stack, CASE WHEN case_optin = 'Да' THEN 1 ELSE 0 END "
            f"FROM users{where}",
            params,
        ).fetchall()
    except sqlite3.Error:
        return None
    yes = [row for row in rows if row[2]]
    fields: dict[str, int] = {}
    stacks: dict[str, int] = {}
    for study, stack, _mark in yes:
        field = _plain(study)
        fields[field] = fields.get(field, 0) + 1
        tokens = _tokens(stack)
        if not tokens:
            stacks[_EMPTY] = stacks.get(_EMPTY, 0) + 1
            continue
        for token in tokens:
            stacks[token] = stacks.get(token, 0) + 1
    counted = len(yes)
    return {
        "count": counted,
        "total": len(rows),
        "percent": _percent(counted, len(rows)),
        "fields": _bars(fields, counted),
        "stack": _bars(stacks, counted),
    }
