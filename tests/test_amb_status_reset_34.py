"""Сброс мигрированных статусов амбассадора (`services/amb/amb_status_reset.py`).

Общие помощники (временная БД, посев делегатов) — их берёт tests/test_amb_reset_admin.py, где
проверяется сама кнопка «🧹 Сбросить статусы». Здесь — сторож сервиса: сообщений он не шлёт и
пишет только через единственного писателя `amb_status_db.set_status`.
Async — через `asyncio.run()`, временная БД — тот же приём, что tests/test_amb_status_34.py.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import asyncio
import sqlite3
from pathlib import Path


from config import config
from database import amb_status_db as sdb
from database import db
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
PAST = "RT 25"
AT = "2026-09-30 12:00:00"
REPO = REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_amb_status_reset_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, status, *, season=SEASON):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "season": season,
    }))
    _run(db.set_user_status(tid, "approved"))
    if status:
        assert _run(sdb.set_status(tid, status, at=AT))


def _status(tid):
    return _sql("SELECT ambassador_status, is_ambassador FROM users WHERE telegram_id = ?",
                (tid,))[0]


def _world(tmp_path):
    _ready(tmp_path)
    _seed(1, "candidate")                 # кандидат текущего сезона
    _seed(2, "active")                    # амбассадор текущего сезона
    _seed(3, "candidate", season=PAST)    # кандидат прошлого сезона
    _seed(4, "active", season=PAST)       # амбассадор прошлого сезона
    _seed(5, None, season=PAST)           # без статуса — не трогаем
    _seed(6, "candidate")                 # ещё кандидат текущего сезона


def test_service_sends_nothing_and_writes_only_through_set_status():
    src = (REPO / "services" / "amb" / "amb_status_reset.py").read_text(encoding="utf-8")
    for marker in ("send_message", "aiogram", "send_or_queue", "coins"):
        assert marker not in src, marker
    assert "set_status(" in src
    assert "UPDATE" not in src.upper().replace("UPDATED", "")
