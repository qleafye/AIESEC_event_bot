"""Подтверждение удаления сессии программы называет, что пропадёт: отметки на сессии, оценки,
приглашение оценить — и подсказывает правку вместо пересоздания (CLAUDE.md: разрушительные
операции — с подтверждением, в котором написано, что именно пропадёт)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers import admin_program
from services.program import point_for_session
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 961003101
DELEGATES = (961003110, 961003111)


def _run(coro):
    return asyncio.run(coro)


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.edited = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text


class _Callback:
    def __init__(self, data):
        self.data = data
        self.from_user = _User(SUPERADMIN_ID)
        self.message = _Msg()

    async def answer(self, text=None, show_alert=False):
        pass


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "program_delete_confirm.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _confirm_text(sid: int) -> str:
    cb = _Callback(f"prog_d:{sid}")
    _run(admin_program.prog_delete_confirm(cb))
    return cb.message.edited


def test_confirm_names_checkins_and_ratings_that_will_be_lost(tmp_path):
    _ready(tmp_path)
    sid = _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    for tid in DELEGATES:
        _run(db.add_user({"telegram_id": tid, "full_name": "Д", "registration_date": "2026-09-01",
                          "event_city": "spb", "status": "approved"}))
        _run(db.record_checkin(tid, point_for_session(sid), source="scan"))
    _run(db.create_session_feedback_prompt(DELEGATES[0], sid, "2026-10-03 11:10:00"))
    _run(db.set_session_feedback_rating(DELEGATES[0], sid, 5, "2026-10-03 11:15:00"))
    text = _confirm_text(sid)
    assert "отметок на сессии — 2" in text
    assert "оценок — 1" in text
    assert "✏ Время" in text  # подсказка: поправить, а не пересоздавать


def test_confirm_without_checkins_has_no_empty_counters(tmp_path):
    _ready(tmp_path)
    sid = _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    text = _confirm_text(sid)
    assert "Пропадут из отчёта" not in text
    assert "пропадёт из программы" in text
