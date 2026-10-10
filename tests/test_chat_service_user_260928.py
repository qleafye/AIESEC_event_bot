"""Служебный 777000 («Telegram»: автопересылки связанного канала) не в счёт.

До выката живого рейтинга автопересылки засчитывались ему в chat_activity как делегату;
новые строки не пишутся, но накопленные остались. Их не показывают ни календарь сообщений
на дашборде, ни итоги, ни рейтинг.
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from dashboard import queries
from database import db
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db

CHAT = -1009286001
SERVICE = 777000


def _ready(tmp_path):
    path = str(tmp_path / "service.db")
    config.DB_PATH = path
    config.ADMIN_IDS = [1]
    fast_init_db()
    today = msk_now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO chat_activity (chat_id, telegram_id, day, messages) VALUES (?, ?, ?, ?)",
        [(CHAT, SERVICE, today, 40), (CHAT, 11, today, 3)],
    )
    conn.executemany(
        "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len) "
        "VALUES (?, ?, ?, ?, 'text', 50)",
        [(CHAT, 1, SERVICE, f"{today} 10:00:00"), (CHAT, 2, 11, f"{today} 10:01:00")],
    )
    conn.commit()
    conn.close()
    return path


def test_daily_messages_chart_skips_service_user(tmp_path):
    path = _ready(tmp_path)
    with dash_db.read_conn(path) as conn:
        rows = queries.chat_messages_daily(conn, CHAT)
    assert sum(cnt for _day, cnt in rows) == 3


def test_activity_totals_skip_service_user(tmp_path):
    _ready(tmp_path)
    totals = asyncio.run(db.chat_activity_totals(CHAT))
    assert totals == {"today": 3, "week": 3}


def test_rating_skips_service_user(tmp_path):
    path = _ready(tmp_path)
    with dash_db.read_conn(path) as conn:
        result = chat_rating.chat_rating(conn, {"chat_id": CHAT, "city": None}, period="all",
                                         admin_ids=set(), now=msk_now().replace(tzinfo=None),
                                         registered_only=False)
    assert {r["telegram_id"] for r in result["rows"]} == {11}
