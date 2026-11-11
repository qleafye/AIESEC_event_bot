"""Ответ «дойду» и список одобренных, которых нет в чате города.

pytest-asyncio нет — корутины через asyncio.run.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from config import config
from database import db
from handlers.chat import admin_city_chat as screen
from handlers.chat import coming_reply
from keyboards.builders import get_main_menu_kb
from services import chat_tracking
from tests._dbtpl import fast_init_db

ADMIN = 1
CHAT = -100551


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "coming.db")
    config.ADMIN_IDS = [ADMIN]
    fast_init_db()
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("event_season", "YL26"))
    return config.DB_PATH


async def _user(uid, name, *, city="msk", status="approved", season="YL26"):
    await db.add_user({
        "telegram_id": uid,
        "full_name": name,
        "registration_date": "2026-11-01 00:00:00",
        "event_city": city,
        "season": season,
    })
    await db.set_user_status(uid, status)
    if season is None:
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET season = NULL WHERE telegram_id = ?", (uid,),
            )
            await conn.commit()


def _texts(kb):
    return [b.text for row in kb.keyboard for b in row]


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid):
        self.from_user = _User(uid)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def test_attendance_buttons_only_for_approved(tmp_path):
    _ready(tmp_path)
    _run(_user(551, "Ждёт", status="pending"))
    _run(_user(552, "Анна"))
    pending = _texts(_run(get_main_menu_kb(551)))
    approved = _texts(_run(get_main_menu_kb(552)))
    assert "Дойду" not in pending and "Не дойду" not in pending
    assert "Дойду" in approved and "Не дойду" in approved
    assert "Дойду" not in _texts(_run(get_main_menu_kb()))


def test_attendance_reply_stores_without_asking_arrival_time(tmp_path):
    _ready(tmp_path)
    _run(_user(553, "Борис"))
    msg = _Msg(553)
    _run(coming_reply.coming_yes(msg))
    user = _run(db.get_user(553))
    assert user["coming_reply"] == "дойду"
    assert user["attendance_format"] in (None, "")
    assert msg.answers == ["Записала: дойду."]
    assert "час" not in msg.answers[0].lower()
    src = Path("handlers/chat/coming_reply.py").read_text(encoding="utf-8")
    assert "ban_chat_member" not in src
    assert "время приезда" not in src


def test_attendance_pending_tap_does_not_store(tmp_path):
    _ready(tmp_path)
    _run(_user(554, "Вера", status="pending"))
    msg = _Msg(554)
    _run(coming_reply.coming_no(msg))
    user = _run(db.get_user(554))
    assert user["coming_reply"] in (None, "")
    assert "одобрен" in msg.answers[0].lower()


def test_city_chat_lists_approved_absent_and_hides_member(tmp_path, monkeypatch):
    _ready(tmp_path)
    import domain.cities as cities
    assert _run(cities.set_admin_city(ADMIN, "msk"))
    _run(chat_tracking.bind_chat(ADMIN, CHAT, "Московский чат", "msk"))
    _run(_user(561, "Анна"))
    _run(_user(562, "Борис"))
    _run(_user(563, "Галя", city="spb"))
    _run(_user(564, "Дима", status="pending"))
    _run(_user(565, "Ева", season="OLD"))
    _run(_user(566, "Женя", season=None))
    _run(db.set_coming_reply(561, "дойду"))
    _run(db.upsert_chat_member(CHAT, 562, "member", "test"))
    _run(db.upsert_chat_member(CHAT, 561, "left", "test"))

    async def _describe(client, city):
        return "Живой ответ площадки."

    monkeypatch.setattr(screen, "describe", _describe)
    text, _kb = _run(screen.render_city_chat_screen(ADMIN, "msk", object()))
    assert "Анна" in text and "дойду" in text
    assert "Женя" in text and "ещё не ответил" in text
    assert "Борис" not in text
    assert "Галя" not in text
    assert "Дима" not in text
    assert "Ева" not in text
    src = Path("handlers/chat/admin_city_chat.py").read_text(encoding="utf-8")
    assert "дойду" in src
    assert "ban_chat_member" not in src
    assert "kick" not in src.lower()
