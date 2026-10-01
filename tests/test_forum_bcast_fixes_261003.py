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


# ── «Написать не пришедшим»: тихие часы не мешают в день форума ──────────────────────────────

class TextBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = dict(fail_for)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        exc = self.fail_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text, reply_markup))
        return type("Msg", (), {"message_id": 1})()


def _quiet_all_day():
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "00:00"))
    _run(db.set_setting("quiet_hours_end", "23:59"))


def test_not_arrived_ignores_quiet_hours_on_forum_day(tmp_path, monkeypatch):
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    _quiet_all_day()
    bot = TextBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cna, "msk_now", lambda: datetime(2026, 10, 3, 8, 30))
    res = _run(cna.send(city=None, city_scope=None))
    assert res["sent"] == 1 and res["quiet"] == 0


def test_not_arrived_keeps_quiet_hours_on_other_days(tmp_path, monkeypatch):
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    _quiet_all_day()
    bot = TextBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cna, "msk_now", lambda: datetime(2026, 10, 6, 8, 30))
    res = _run(cna.send(city=None, city_scope=None))
    assert res["sent"] == 0 and res["quiet"] == 1 and bot.sent == []


def test_not_arrived_transient_failure_unmarks_for_retry(tmp_path, monkeypatch):
    """Сбой отправки не исключает делегата навсегда: повторное нажатие берёт его снова."""
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    monkeypatch.setattr(cna, "msk_now", lambda: datetime.now())
    bot = TextBot(fail_for={1: RuntimeError("network down")})
    monkeypatch.setattr(sched, "_bot", bot)
    res = _run(cna.send(city=None, city_scope=None))
    assert res["failed"] == 1 and res["sent"] == 0
    assert _run(db.checkin_not_arrived_pending_ids()) == [1]
    bot.fail_for.clear()
    res2 = _run(cna.send(city=None, city_scope=None))
    assert res2["sent"] == 1


def test_not_arrived_blocked_user_stays_marked(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    monkeypatch.setattr(cna, "msk_now", lambda: datetime.now())
    bot = TextBot(fail_for={1: TelegramForbiddenError(method=None, message="bot was blocked")})
    monkeypatch.setattr(sched, "_bot", bot)
    res = _run(cna.send(city=None, city_scope=None))
    assert res["failed"] == 1
    assert _run(db.checkin_not_arrived_pending_ids()) == []
