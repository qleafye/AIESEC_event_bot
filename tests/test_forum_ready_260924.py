"""Бэклог чек-ина №25: «🚦 Готовность к форуму» (handlers/admin_forum_ready.py) — светофор
по городу из хаба «🎪 Форум: функции». Экран только читает: планировщик — через `get_job`,
без `schedule_city_jobs` (тот переставляет джобы).

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_forum_functions as aff
from handlers import admin_forum_ready as afr
from handlers.admin_caps import role_caps_key
from services import sheets
from tests._dbtpl import fast_init_db

ADMIN_ID = 924200


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum_ready_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


class _Bot:
    id = 42

    def __init__(self, member=True):
        self.member = member

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member" if self.member else "left")


class _FakeSched:
    def __init__(self, jobs=None):
        self.jobs = jobs or {}
        self.added = []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, *a, **k):  # экран не имеет права ставить джобы
        self.added.append((a, k))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _patch_sched(monkeypatch, sched):
    from services import scheduler
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: sched)


def test_empty_setup_is_red_with_fix_buttons(tmp_path, monkeypatch):
    _ready(tmp_path)
    sched = _FakeSched()
    _patch_sched(monkeypatch, sched)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🔴 Дата форума не задана" in text
    assert "🔴 Вход по QR выключен" in text
    assert "🔴 Никому, кроме суперадминов, не выдано право отметки" in text
    assert "🔴 Программа не заведена" in text
    assert "⚪ Таблица не подключена" in text
    assert "🟡 Чат делегатов не привязан" in text
    cbs = _cbs(kb)
    for cb in ("settings_edit:forum_date", "admin_forum_functions", "admin_roles", "admin_program", "admin_sos"):
        assert cb in cbs
    assert cbs[-2:] == ["forum_ready_re:msk", "admin_forum_functions"]
    assert sched.added == []


def test_ready_city_is_green(tmp_path, monkeypatch):
    _ready(tmp_path)
    from datetime import datetime
    run_at = datetime(2026, 10, 2, 18, 0)
    _patch_sched(monkeypatch, _FakeSched({"checkin_qr_evening:all": SimpleNamespace(next_run_time=run_at)}))
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.add_staff(924201, "stats_manager", ADMIN_ID))
    _run(db.set_setting(role_caps_key("stats_manager"), "checkin"))
    _run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting("sos_chat_id", "-1001"))
    _run(db.set_setting("sos_chat_title", "Орги"))
    _run(db.set_setting("delegate_chat_id", "-1002"))
    _run(db.set_setting("delegate_chat_title", "Делегаты"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    monkeypatch.setattr(sheets, "_write_state", {"ok": time.time() - 120, "fail": None})

    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🔴" not in text and "🟡" not in text, text
    assert "рассылка QR запланирована на 02.10 18:00" in text
    assert "С правом отметки на входе: 1 чел." in text
    assert "сессий 1" in text
    assert "Чат SOS «Орги», бот в чате" in text
    assert "последняя запись 2 мин назад" in text
    assert "Всё готово." in text
    assert _cbs(kb) == ["forum_ready_re:msk", "admin_forum_functions"]


def test_bot_kicked_from_sos_chat_and_sheet_failure_are_red(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    _run(db.set_setting("sos_chat_id", "-1001"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    now = time.time()
    monkeypatch.setattr(sheets, "_write_state", {"ok": now - 600, "fail": now - 60})
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot(member=False)))
    assert "🔴 Бота нет в чате SOS" in text
    assert "🔴 Последняя запись в таблицу не прошла" in text
    assert "admin_sync_sheet" in _cbs(kb)


def test_qr_on_without_jobs_or_date_explains(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    _run(db.set_setting("checkin_qr_enabled", "on"))
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🔴 Вход по QR включён, но рассылка не поставлена — нет даты форума" in text
    assert "checkinqr_cfg:msk" in _cbs(kb)


def test_scheduler_down_does_not_break_screen(tmp_path, monkeypatch):
    _ready(tmp_path)
    from services import scheduler

    def _boom():
        raise RuntimeError("no scheduler")

    monkeypatch.setattr(scheduler, "get_scheduler", _boom)
    _run(db.set_setting("checkin_qr_enabled", "on"))
    text, _kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "расписание рассылки прочитать не удалось" in text


def test_note_write_records_success_and_failure(monkeypatch):
    monkeypatch.setattr(sheets, "_write_state", {"ok": None, "fail": None})
    sheets._note_write(True)
    sheets._note_write(False)
    state = sheets.last_write_state()
    assert state["ok"] and state["fail"] and state["fail"] >= state["ok"]


def test_hub_has_ready_button(tmp_path):
    _ready(tmp_path)
    _text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert _cbs(kb)[0] == "forum_ready:msk"


def test_one_failing_row_turns_gray_and_screen_still_renders(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")

    async def _boom(code):
        raise RuntimeError("db locked")

    monkeypatch.setattr(afr, "_row_program", _boom)
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "⚪ Программа: не удалось проверить" in text
    assert "🔴 Дата форума не задана" in text
    assert "🟡 Чат делегатов не привязан" in text
    assert text.count("\n🔴 ") + text.count("\n🟡 ") + text.count("\n🟢 ") + text.count("\n⚪ ") == 7
    assert "forum_ready_re:msk" in _cbs(kb)


def test_qr_send_counts_failure_is_contained(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    _run(db.set_setting("checkin_qr_enabled", "on"))

    async def _boom(**k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(afr, "checkin_qr_send_counts", _boom)
    text, _kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "⚪ Вход по QR: не удалось проверить" in text
    assert "Дата форума" in text


def test_count_program_sessions_none_counts_all_cities(tmp_path):
    from services.checkin_arrival import count_program_sessions
    _ready(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "А"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Б"))
    assert _run(count_program_sessions("spb")) == 1
    assert _run(count_program_sessions(None)) == 2
