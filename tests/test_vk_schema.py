"""Карточка делегата хранит номер ВК. Телеграм может быть пустым."""
import asyncio
import sqlite3

import pytest

from config import config
from database import db


def _use_tmp_db(tmp_path):
    db_file = tmp_path / "vk_schema.db"
    config.DB_PATH = str(db_file)
    return db_file


def test_vk_id_nullable(tmp_path):
    db_file = _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO users (vk_id, full_name) VALUES (?, ?)", (123, "Только ВК"))
    conn.commit()
    row = conn.execute(
        "SELECT telegram_id, vk_id, full_name FROM users WHERE vk_id = 123"
    ).fetchone()
    info = conn.execute("PRAGMA table_info(users)").fetchall()
    conn.close()
    assert row == (None, 123, "Только ВК")
    vk_col = next(col for col in info if col[1] == "vk_id")
    assert vk_col[3] == 0  # notnull


def test_both_ids_nullable(tmp_path):
    db_file = _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO users (telegram_id, full_name) VALUES (?, ?)", (456, "Только ТГ"))
    conn.execute("INSERT INTO users (vk_id, full_name) VALUES (?, ?)", (123, "Только ВК"))
    conn.execute("INSERT INTO users (vk_id, full_name) VALUES (?, ?)", (124, "Ещё ВК"))
    conn.execute(
        "INSERT INTO users (telegram_id, vk_id, full_name) VALUES (?, ?, ?)",
        (789, 999, "Связан"),
    )
    conn.commit()
    tg = conn.execute("SELECT vk_id FROM users WHERE telegram_id = 456").fetchone()
    vk = conn.execute("SELECT telegram_id FROM users WHERE vk_id = 123").fetchone()
    both = conn.execute(
        "SELECT telegram_id, vk_id FROM users "
        "WHERE telegram_id IS NOT NULL AND vk_id IS NOT NULL"
    ).fetchall()
    conn.close()
    assert tg == (None,)
    assert vk == (None,)
    assert both == [(789, 999)]


def test_query_vk_users(tmp_path):
    db_file = _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO users (telegram_id) VALUES (?)", (456,))
    conn.execute("INSERT INTO users (vk_id) VALUES (?)", (123,))
    conn.execute("INSERT INTO users (telegram_id, vk_id) VALUES (?, ?)", (789, 999))
    conn.commit()
    rows = conn.execute(
        "SELECT vk_id FROM users WHERE vk_id IS NOT NULL ORDER BY vk_id"
    ).fetchall()
    conn.close()
    assert rows == [(123,), (999,)]


def test_link_vk_account_rejects_second_link(tmp_path):
    db_file = _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO users (telegram_id, full_name) VALUES (?, ?)", (456, "ТГ"))
    conn.execute("INSERT INTO users (telegram_id, full_name) VALUES (?, ?)", (457, "Другой"))
    conn.commit()
    conn.close()
    asyncio.run(db.link_vk_account(456, 123))
    linked = sqlite3.connect(db_file).execute(
        "SELECT vk_id FROM users WHERE telegram_id = 456"
    ).fetchone()
    assert linked == (123,)
    with pytest.raises(ValueError, match="Аккаунт уже связан"):
        asyncio.run(db.link_vk_account(456, 124))
    with pytest.raises(ValueError, match="Аккаунт уже связан"):
        asyncio.run(db.link_vk_account(457, 123))


def test_old_primary_key_keeps_telegram_row(tmp_path):
    db_file = _use_tmp_db(tmp_path)
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT, registration_date TEXT)"
    )
    conn.execute(
        "INSERT INTO users (telegram_id, full_name, registration_date) VALUES (?, ?, ?)",
        (111, "Старый", "2025-01-01"),
    )
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())

    conn = sqlite3.connect(db_file)
    kept = conn.execute(
        "SELECT full_name, status FROM users WHERE telegram_id = 111"
    ).fetchone()
    conn.execute("INSERT INTO users (vk_id, full_name) VALUES (?, ?)", (50, "Только ВК"))
    conn.commit()
    fresh = conn.execute(
        "SELECT telegram_id FROM users WHERE vk_id = 50"
    ).fetchone()
    pk = next(
        col[5] for col in conn.execute("PRAGMA table_info(users)") if col[1] == "telegram_id"
    )
    conn.close()
    assert kept == ("Старый", "approved")
    assert fresh == (None,)
    assert pk == 0
