"""Phase 15 (D-04, T-15-03-01): read-only доступ к БД бота.

Отдельный от `database/db.py` модуль — тот использует `aiosqlite` (async, подключение,
физически способное писать); этот — синхронный stdlib `sqlite3` с URI `mode=ro`:
подключение физически не может выполнить запись, даже если в коде дашборда будет баг.
`:ro`-монтирование тома (план 15-06) снято: в WAL-режиме (Phase 17) читателю нужен доступ
к `-shm` в каталоге БД, и на read-only томе подключение падало «unable to open database
file». Единственный рубеж — этот модуль.

Ни одной функции записи в этом модуле нет и не будет.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager


@contextmanager
def read_conn(db_path: str):
    """Контекст-менеджер: `sqlite3.Connection`, открытое строго на чтение (`mode=ro`).

    `PRAGMA busy_timeout=5000` — второй рубеж поверх WAL (Phase 17 уже включил WAL в файле
    бота): если бот держит транзакцию записи чуть дольше кванта, читатель ждёт вместо
    немедленного `sqlite3.OperationalError: database is locked`.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()
