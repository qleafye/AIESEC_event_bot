"""D-31 снял NOT NULL с `sos_reports.category` только в `CREATE TABLE IF NOT EXISTS` — на
живых базах со старой схемой `create_sos_report` без категории падал на NOT NULL. init_db
обязан пересоздать таблицу, сохранив строки, и не трогать её при повторном запуске."""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db

# Схема sos_reports на 24d6ab0 (до D-31) — дословно.
_OLD_DDL = """
    CREATE TABLE IF NOT EXISTS sos_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        city TEXT,
        category TEXT NOT NULL,
        details_text TEXT,
        details_photo_file_id TEXT,
        latitude REAL,
        longitude REAL,
        chat_id INTEGER,
        card_message_id INTEGER,
        claimed_by INTEGER,
        claimed_by_name TEXT,
        claimed_at TEXT,
        resolved_by INTEGER,
        resolved_by_name TEXT,
        resolved_at TEXT,
        escalated_at TEXT,
        created_at TEXT NOT NULL,
        delivery_failed_at TEXT,
        prior_open_report_id INTEGER
    )
"""


def _category_notnull(conn) -> int:
    return next(c[3] for c in conn.execute("PRAGMA table_info(sos_reports)") if c[1] == "category")


def test_init_db_relaxes_old_sos_category_not_null(tmp_path):
    config.DB_PATH = str(tmp_path / "old_sos.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(_OLD_DDL)
    conn.execute(
        "INSERT INTO sos_reports (id, telegram_id, city, category, details_text, created_at) "
        "VALUES (7, 111, 'spb', 'medical', 'плохо', '2026-09-20 10:00:00')"
    )
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())

    conn = sqlite3.connect(config.DB_PATH)
    assert _category_notnull(conn) == 0
    ddl_after_first = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'sos_reports'"
    ).fetchone()[0]
    conn.close()

    new_id = asyncio.run(db.create_sos_report(222, "spb"))
    assert new_id > 7  # счётчик AUTOINCREMENT не откатился

    old = asyncio.run(db.get_sos_report(7))
    assert old["category"] == "medical" and old["details_text"] == "плохо"
    assert old["telegram_id"] == 111

    # Повторный init_db — ничего не пересоздаёт.
    asyncio.run(db.init_db())
    conn = sqlite3.connect(config.DB_PATH)
    assert conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'sos_reports'"
    ).fetchone()[0] == ddl_after_first
    assert conn.execute("SELECT COUNT(*) FROM sos_reports").fetchone()[0] == 2
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'sos_reports'"
    )}
    assert {"idx_sos_reports_telegram_id", "idx_sos_reports_city"} <= idx
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'sos_reports_new'").fetchone() is None
    conn.close()
