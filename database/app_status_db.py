"""Разбивка списка получателей рассылки по статусу заявки.

Соединение — через атрибут модуля `_db._connect()` (тесты подменяют `config.DB_PATH`).
"""
from __future__ import annotations

from database import db as _db

APPROVED = "approved"
PENDING = "pending"
REJECTED = "rejected"
NOT_SUBMITTED = "not_submitted"


async def split_ids_by_app_status(ids: list[int]) -> dict[str, list[int]]:
    """{approved, pending, rejected, not_submitted: [id, ...]} в исходном порядке id.

    Строка в `users` появляется только при подаче анкеты, поэтому id без строки — «не подал
    анкету». Пустой или незнакомый статус считается «на рассмотрении»: он точно не одобрен."""
    ids = [int(i) for i in ids]
    status: dict[int, str] = {}
    async with _db._connect() as db:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            async with db.execute(
                f"SELECT telegram_id, status FROM users WHERE telegram_id IN ({marks})", chunk,
            ) as cursor:
                for tid, st in await cursor.fetchall():
                    status[int(tid)] = st if st in (APPROVED, REJECTED) else PENDING
    out: dict[str, list[int]] = {APPROVED: [], PENDING: [], REJECTED: [], NOT_SUBMITTED: []}
    for i in ids:
        out[status.get(i, NOT_SUBMITTED)].append(i)
    return out
