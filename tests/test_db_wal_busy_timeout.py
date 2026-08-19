"""SQLite concurrency posture: WAL journaling (set once in init_db, persistent) and a busy
timeout on EVERY connection — async (database.db._connect) and the sync sqlite3 reads in
services/sheets.py. Without these, one writer holding the lock makes a concurrent reader
fail instantly with "database is locked"."""
import asyncio
import re
import sqlite3
from pathlib import Path

import aiosqlite

from config import config
from database import db


def _init(tmp_path) -> str:
    path = str(tmp_path / "wal_test.db")
    config.DB_PATH = path
    asyncio.run(db.init_db())
    return path


def test_init_db_switches_file_to_wal(tmp_path):
    path = _init(tmp_path)

    async def _mode():
        async with aiosqlite.connect(path) as conn:  # plain connection: mode is persistent
            async with conn.execute("PRAGMA journal_mode") as cur:
                return (await cur.fetchone())[0]

    assert asyncio.run(_mode()).lower() == "wal"


def test_connect_helper_sets_busy_timeout(tmp_path):
    _init(tmp_path)

    async def _timeout():
        async with db._connect() as conn:
            async with conn.execute("PRAGMA busy_timeout") as cur:
                return (await cur.fetchone())[0]

    assert asyncio.run(_timeout()) == db.DB_BUSY_TIMEOUT_MS == 5000


def test_db_module_has_no_bare_aiosqlite_connect():
    """All 100+ connections must go through _connect(); a bare aiosqlite.connect would
    silently skip the busy timeout."""
    src = Path(db.__file__).read_text(encoding="utf-8")
    bare = [ln for ln in src.splitlines() if "aiosqlite.connect(" in ln and "def _connect" not in ln]
    assert len(bare) == 1, bare  # exactly the one inside _connect()
    assert "timeout=DB_BUSY_TIMEOUT_MS" in bare[0]


def test_no_bare_db_path_connects_outside_db_module():
    root = Path(db.__file__).resolve().parent.parent
    offenders = []
    for py in list((root / "services").glob("*.py")) + list((root / "handlers").glob("*.py")) \
            + list((root / "scripts").glob("*.py")) + [root / "main.py"]:
        for ln in py.read_text(encoding="utf-8").splitlines():
            if re.search(r"aiosqlite\.connect\(\s*config\.DB_PATH\s*\)", ln):
                offenders.append(f"{py.name}: {ln.strip()}")
            if re.search(r"sqlite3\.connect\(\s*config\.DB_PATH\s*\)", ln):
                offenders.append(f"{py.name}: {ln.strip()}")
    assert offenders == []


def test_sheets_sync_reads_use_busy_timeout(tmp_path, monkeypatch):
    """services.sheets opens plain sqlite3 connections from a worker thread; they must pass
    the same timeout so a concurrent commit makes them wait, not fail."""
    from services import sheets

    _init(tmp_path)
    seen = {}
    real_connect = sqlite3.connect

    def spy(path, *a, **kw):
        seen["timeout"] = kw.get("timeout")
        return real_connect(path, *a, **kw)

    monkeypatch.setattr(sheets.sqlite3, "connect", spy)
    sheets._load_main_tab_setting()
    assert seen.get("timeout") == sheets._DB_BUSY_TIMEOUT_S == 5.0
