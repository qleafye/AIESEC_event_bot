"""Phase 15 Plan 03 (STAT-01/STAT-04, D-04): каркас пакета `dashboard/` и read-only
подключение к БД бота.

Task 1 — `dashboard/config.py::load_config` (плоская конфигурация, без pydantic) и
`dashboard/db.py::read_conn` (sqlite3, URI `mode=ro`). Никакого pytest-asyncio — весь
модуль синхронный (plain `sqlite3`), в отличие от бота.
"""
import sqlite3
from pathlib import Path

import pytest

from dashboard.config import load_config
from dashboard.db import read_conn

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def _make_db(tmp_path, name="ro.db") -> str:
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO t (value) VALUES ('hello')")
    conn.commit()
    conn.close()
    return path


# ── read_conn: read-only enforcement (T-15-03-01) ────────────────────────────────────────

def test_read_conn_reads_existing_data(tmp_path):
    path = _make_db(tmp_path)
    with read_conn(path) as conn:
        row = conn.execute("SELECT value FROM t WHERE id = 1").fetchone()
        assert row["value"] == "hello"


@pytest.mark.parametrize("sql", [
    "INSERT INTO t (value) VALUES ('nope')",
    "UPDATE t SET value = 'nope' WHERE id = 1",
    "DELETE FROM t WHERE id = 1",
    "CREATE TABLE t2 (id INTEGER)",
])
def test_read_conn_cannot_write(tmp_path, sql):
    path = _make_db(tmp_path)
    with read_conn(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(sql)


def test_read_conn_sets_busy_timeout(tmp_path):
    path = _make_db(tmp_path)
    with read_conn(path) as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000


def test_read_conn_closes_connection(tmp_path):
    path = _make_db(tmp_path)
    with read_conn(path) as conn:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


# ── load_config ───────────────────────────────────────────────────────────────────────

def test_load_config_without_session_secret_raises():
    with pytest.raises(RuntimeError):
        load_config(env={})


def test_load_config_with_session_secret_returns_values():
    env = {
        "DASHBOARD_SESSION_SECRET": "s3cr3t",
        "DASHBOARD_DB_PATH": "data/other.db",
        "DASHBOARD_PUBLIC_URL": "https://yl26.alekseev.info",
        "DASHBOARD_BOT_USERNAME": "YouLead_bot",
        "BOT_TOKEN": "123:abc",
        "ADMIN_IDS": "[12345678, 87654321]",
        "PROXY_URL": "socks5://127.0.0.1:1080",
        "EVENT_CITY_DEFAULT": "spb",
    }
    cfg = load_config(env=env)
    assert cfg.session_secret == "s3cr3t"
    assert cfg.db_path == "data/other.db"
    assert cfg.public_url == "https://yl26.alekseev.info"
    assert cfg.bot_username == "YouLead_bot"
    assert cfg.bot_token == "123:abc"
    assert cfg.admin_ids == (12345678, 87654321)
    assert cfg.proxy_url == "socks5://127.0.0.1:1080"
    assert cfg.event_city_default == "spb"


def test_load_config_defaults_when_optional_keys_absent():
    cfg = load_config(env={"DASHBOARD_SESSION_SECRET": "s3cr3t"})
    assert cfg.db_path == "data/forum.db"
    assert cfg.public_url == ""
    assert cfg.bot_username == ""
    assert cfg.admin_ids == ()
    assert cfg.proxy_url is None
    assert cfg.event_city_default == "msk"


def test_load_config_admin_ids_skips_garbage_tokens():
    cfg = load_config(env={"DASHBOARD_SESSION_SECRET": "s", "ADMIN_IDS": "[1, oops, 2]"})
    assert cfg.admin_ids == (1, 2)


# ── structural guard: dashboard/*.py stays aiogram/handlers/gspread-free ────────────────

def test_dashboard_package_never_imports_bot_frameworks():
    forbidden = ("import aiogram", "from aiogram", "from handlers", "import handlers", "import gspread")
    offenders = []
    for py_file in DASHBOARD_DIR.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{py_file.name}: {token!r}")
    assert not offenders, f"dashboard/ must stay framework-free: {offenders}"
