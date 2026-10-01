"""Рассылки дня форума (форумы 03.10): тексты, догон, повторные попытки, отметки «отправлено».

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

from config import config
from database import db
import services.checkin_broadcast as cb
import services.scheduler as sched
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="forum_bcast_fixes.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.set_setting("event_season", "YL 26/2"))
    _run(db.set_setting("forum_date", "03.10.2026"))


def _seed(tid, city=None, name=None):
    _run(db.add_user({
        "telegram_id": tid, "full_name": name or f"D{tid}",
        "registration_date": "2026-09-01 00:00:00", "event_city": city,
    }))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status='approved', season='YL 26/2' WHERE telegram_id=?", (tid,))
    conn.commit()
    conn.close()


class PhotoBot:
    def __init__(self):
        self.photos = []

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.photos.append((chat_id, caption, reply_markup))
        return type("Msg", (), {"message_id": 1})()


# ── Утренний повтор: «Сегодня форум», не «Завтра форум» ──────────────────────────────────────

def test_morning_repeat_uses_today_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    _run(cb.send_morning_repeat(None))
    caption = bot.photos[0][1]
    assert caption.startswith("Сегодня форум!") and "Завтра" not in caption


def test_manual_send_on_forum_day_uses_today_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 30))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Сегодня форум!")


def test_evening_send_keeps_tomorrow_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Завтра форум!")


def test_morning_text_registered_like_neighbours():
    from handlers.admin_settings import SETTINGS_FIELDS
    from services.i18n_form_manual import FORM_DEFAULT_EN
    from settings_schema import SETTINGS_SCHEMA
    from settings_synonyms import SETTINGS_SYNONYMS

    entry = SETTINGS_SCHEMA["checkin_qr_morning_text"]
    assert entry["group"] == "reg" and entry["per_city"] is True
    assert "завтра" not in entry["default"].lower()
    assert entry["default"] in FORM_DEFAULT_EN
    assert "checkin_qr_morning_text" in SETTINGS_SYNONYMS
    assert "checkin_qr_morning_text" in {k for k, _l, _p in SETTINGS_FIELDS}
