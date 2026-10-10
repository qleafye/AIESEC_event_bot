"""Идея №16 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): «📊 Отчёт дня
форума» вечером — `services/forum_day_report.py` + экран `handlers/admin_forum_functions.py`.

Стиль — `tests/test_checkin_volunteer_broadcast_260924.py` (реальный AsyncIOScheduler на
временном jobstore, `asyncio.run`, шаблонная БД `tests/_dbtpl.fast_init_db`)."""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db
import services.scheduler as sched
import services.forum_day_report as fdr
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, FakeMessage

ADMIN_ID = 924101
UID = 924110


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="forum_day_report.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _set(key, value):
    await db.set_setting(key, value)


async def _add_delegate(tid, *, city=None, status="approved"):
    await db.add_user({
        "telegram_id": tid, "full_name": f"Делегат {tid}", "username": "d",
        "event_city": city, "registration_date": "2026-01-01 00:00:00",
        "status": status,
    })


class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text)]

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": 1})()


def _with_bot(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


def _build_scheduler(tmp_path):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.sqlite'}")},
        timezone=sched.MOSCOW_TZ,
    )


def _run_scheduled(tmp_path, monkeypatch, body):
    s = _build_scheduler(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", s)

    async def go():
        s.start(paused=True)
        try:
            return await body(s)
        finally:
            s.shutdown(wait=False)

    return asyncio.run(go())


# ══════════════════════════════════════════════════════════════════════════════════════════
# БД: идемпотентность отправки + USER_PURGE (таблица без telegram_id -- вне purge)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_mark_sent_idempotent_per_city_and_day(tmp_path):
    _ready(tmp_path)
    assert _run(db.forum_day_report_sent_days("msk")) == set()
    marked = _run(db.forum_day_report_mark_sent("msk", "2026-10-30", "2026-10-30 21:00:00"))
    assert marked is True
    assert _run(db.forum_day_report_sent_days("msk")) == {"2026-10-30"}
    marked2 = _run(db.forum_day_report_mark_sent("msk", "2026-10-30", "2026-10-30 21:05:00"))
    assert marked2 is False


def test_mark_sent_sentinel_keeps_cities_off_idempotent(tmp_path):
    """`city=None` (модуль городов выключен) не даёт NULL-дубли — сентинел `_all` держит
    UNIQUE(city, day) осмысленным."""
    _ready(tmp_path)
    assert _run(db.forum_day_report_mark_sent(None, "2026-10-30", "2026-10-30 21:00:00")) is True
    assert _run(db.forum_day_report_mark_sent(None, "2026-10-30", "2026-10-30 21:00:00")) is False
    assert _run(db.forum_day_report_sent_days(None)) == {"2026-10-30"}
    assert _run(db.forum_day_report_sent_days("msk")) == set()  # другой город не задет


# ══════════════════════════════════════════════════════════════════════════════════════════
# БД: агрегаты отчёта
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_count_checkins_by_point_and_day_scopes_by_day(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:00:00"))
    assert _run(db.count_checkins_by_point_and_day(db.CHECKIN_ENTRY_POINT, "2026-10-30")) == 1
    assert _run(db.count_checkins_by_point_and_day(db.CHECKIN_ENTRY_POINT, "2026-10-31")) == 0


def test_count_checkins_by_point_and_day_scoped_by_city(tmp_path):
    import cities as cities_mod
    _ready(tmp_path)
    _run(_add_delegate(1, city="msk"))
    _run(_add_delegate(2, city="spb"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:00:00"))
    _run(db.record_checkin(2, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:00:00"))
    scope_msk = cities_mod.city_scope("msk")
    assert _run(db.count_checkins_by_point_and_day(db.CHECKIN_ENTRY_POINT, "2026-10-30", city_scope=scope_msk)) == 1


def test_count_checkins_by_point_and_day_counts_second_day_entry_separately(tmp_path):
    """Вход каждый день: делегат с отметками на ОБА дня форума считается в отчёте КАЖДОГО дня
    отдельно (по колонке `checkins.day`, не по вычислению из `scanned_at`) — тот же признак,
    которым `miniapp/routers/checkin.py` уже считает «Пришли N из M» на своём экране."""
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="csv", scanned_at="2026-10-30 10:00:00"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="csv", scanned_at="2026-10-31 09:00:00"))
    assert _run(db.count_checkins_by_point_and_day(db.CHECKIN_ENTRY_POINT, "2026-10-30")) == 1
    assert _run(db.count_checkins_by_point_and_day(db.CHECKIN_ENTRY_POINT, "2026-10-31")) == 1


def test_peak_hour_returns_busiest_hour(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(_add_delegate(2))
    _run(_add_delegate(3))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:05:00"))
    _run(db.record_checkin(2, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:45:00"))
    _run(db.record_checkin(3, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 11:10:00"))
    peak = _run(db.checkin_peak_hour_for_city_day("2026-10-30"))
    assert peak == ("10", 2)


def test_peak_hour_none_when_no_data(tmp_path):
    _ready(tmp_path)
    assert _run(db.checkin_peak_hour_for_city_day("2026-10-30")) is None


def test_list_checkins_for_city_day_contains_delegate_fields(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1, city="msk"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:00:00"))
    rows = _run(db.list_checkins_for_city_day("2026-10-30"))
    assert len(rows) == 1
    assert rows[0]["telegram_id"] == 1
    assert rows[0]["point"] == db.CHECKIN_ENTRY_POINT


def test_sos_day_stats_counts_and_average(tmp_path):
    _ready(tmp_path)
    rid1 = _run(db.create_sos_report(1, "msk", None, None, None, None))
    rid2 = _run(db.create_sos_report(2, "msk", None, None, None, None))
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE sos_reports SET created_at = ? WHERE id = ?", ("2026-10-30 10:00:00", rid1))
    conn.execute("UPDATE sos_reports SET created_at = ? WHERE id = ?", ("2026-10-30 10:00:00", rid2))
    conn.execute(
        "UPDATE sos_reports SET claimed_at = ?, claimed_by = 1, claimed_by_name = 'a' WHERE id = ?",
        ("2026-10-30 10:05:00", rid1),
    )
    conn.execute(
        "UPDATE sos_reports SET resolved_at = ?, resolved_by = 1, resolved_by_name = 'a' WHERE id = ?",
        ("2026-10-30 10:10:00", rid1),
    )
    conn.commit()
    conn.close()
    stats = _run(db.sos_day_stats("2026-10-30"))
    assert stats["total"] == 2
    assert stats["resolved"] == 1
    assert abs(stats["avg_claim_minutes"] - 5.0) < 0.01


def test_sos_day_stats_zero_when_no_sos(tmp_path):
    _ready(tmp_path)
    stats = _run(db.sos_day_stats("2026-10-30"))
    assert stats == {"total": 0, "resolved": 0, "avg_claim_minutes": None}


# ══════════════════════════════════════════════════════════════════════════════════════════
# build_report_text: строки только там, где есть данные
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_build_report_text_has_arrival_line_even_with_zero_data(tmp_path):
    _ready(tmp_path)
    text = _run(fdr.build_report_text(None, "2026-10-30"))
    assert "Пришли 0 из 0" in text
    assert "Пик прихода" not in text
    assert "SOS" not in text
    assert "не пришёл" not in text


def test_build_report_text_includes_peak_and_arrival(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(_add_delegate(2))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:00:00"))
    text = _run(fdr.build_report_text(None, "2026-10-30"))
    assert "Пришли 1 из 2" in text
    assert "Пик прихода" in text


def test_build_report_text_includes_sos_only_when_present(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(1, None, None, None, None, None))
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE sos_reports SET created_at = ? WHERE id = ?", ("2026-10-30 10:00:00", rid))
    conn.commit()
    conn.close()
    text = _run(fdr.build_report_text(None, "2026-10-30"))
    assert "SOS за день: 1 (решено 0)" in text


def test_build_report_text_includes_not_arrived_summary_when_sent(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.checkin_not_arrived_mark_sent(1, None, "2026-10-30 12:00:00"))
    _run(db.record_checkin_not_arrived_response(1, "2026-10-30", db.CNA_COMING, "2026-10-30 12:05:00"))
    text = _run(fdr.build_report_text(None, "2026-10-30"))
    assert "едут 1" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# schedule_city_job: гейты + self-rescheduling
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_disabled_by_default(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        return await fdr.schedule_city_job(None)

    result = _run_scheduled(tmp_path, monkeypatch, body)
    assert result == {"scheduled": False, "reason": "disabled"}


def test_schedule_no_date_when_enabled_without_forum_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_day_report_enabled", "on"))

    async def body(s):
        return await fdr.schedule_city_job(None)

    result = _run_scheduled(tmp_path, monkeypatch, body)
    assert result == {"scheduled": False, "reason": "no_date"}


def test_schedule_picks_first_pending_day_of_window(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_day_report_enabled", "on"))
    _run(_set("forum_date", "30.10.2026"))
    _run(_set("sos_active_days", "2"))
    monkeypatch.setattr(fdr, "msk_now", lambda: datetime(2026, 10, 1, 10, 0))

    async def body(s):
        result = await fdr.schedule_city_job(None)
        assert result["scheduled"] is True
        assert result["day"] == "2026-10-30"
        assert result["run_at"] == datetime(2026, 10, 30, 21, 0)
        assert s.get_job(fdr.job_id(None)) is not None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_skips_to_second_day_once_first_sent(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_day_report_enabled", "on"))
    _run(_set("forum_date", "30.10.2026"))
    _run(_set("sos_active_days", "2"))
    _run(db.forum_day_report_mark_sent(None, "2026-10-30", "2026-10-30 21:00:00"))
    monkeypatch.setattr(fdr, "msk_now", lambda: datetime(2026, 10, 30, 21, 5))

    async def body(s):
        result = await fdr.schedule_city_job(None)
        assert result["scheduled"] is True
        assert result["day"] == "2026-10-31"

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_all_sent_returns_reason(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_day_report_enabled", "on"))
    _run(_set("forum_date", "30.10.2026"))
    _run(_set("sos_active_days", "1"))
    _run(db.forum_day_report_mark_sent(None, "2026-10-30", "2026-10-30 21:00:00"))
    monkeypatch.setattr(fdr, "msk_now", lambda: datetime(2026, 10, 30, 21, 5))

    async def body(s):
        result = await fdr.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "all_sent"}

    _run_scheduled(tmp_path, monkeypatch, body)


def test_run_job_sends_marks_and_reschedules_next_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(_set("forum_day_report_enabled", "on"))
    _run(_set("forum_date", "30.10.2026"))
    _run(_set("sos_active_days", "2"))
    monkeypatch.setattr(fdr, "msk_now", lambda: datetime(2026, 10, 30, 21, 0))
    bot = _with_bot(monkeypatch)

    async def body(s):
        await fdr._run_job(None)
        assert await db.forum_day_report_sent_days(None) == {"2026-10-30"}
        job = s.get_job(fdr.job_id(None))
        assert job is not None  # переставлена на день 31.10

    _run_scheduled(tmp_path, monkeypatch, body)
    assert len(bot.sent) >= 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# send_report: чат SOS + личка держателям moderate_reg
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_report_delivers_to_sos_chat_and_dm(tmp_path, monkeypatch):
    from handlers.admin_caps import role_caps_key, role_enabled_key

    _ready(tmp_path)
    _run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    _run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))
    _run(db.add_staff(UID, "reg_manager", ADMIN_ID))
    _run(db.set_setting("sos_chat_id", "-100500"))
    bot = _with_bot(monkeypatch)

    result = _run(fdr.send_report(None, "2026-10-30", mark_sent=True))
    assert result["chat_delivered"] is True
    assert result["dm_delivered"] >= 1
    assert (-100500) in {cid for cid, _t in bot.sent}
    assert _run(db.forum_day_report_sent_days(None)) == {"2026-10-30"}


def test_send_report_manual_does_not_mark_sent(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    result = _run(fdr.send_report(None, "2026-10-30", mark_sent=False))
    assert result["text"]
    assert _run(db.forum_day_report_sent_days(None)) == set()


# ══════════════════════════════════════════════════════════════════════════════════════════
# CSV
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_checkins_csv_for_city_day_contains_rows(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1, city="msk"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 10:00:00"))
    csv_bytes = _run(fdr.checkins_csv_for_city_day("msk", "2026-10-30"))
    text = csv_bytes.decode("utf-8-sig")
    assert "telegram_id" in text
    assert "1" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр: дефолты/формат/TOGGLE_SECTION
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_registry_defaults_and_format():
    from domain.settings.schema import SETTINGS_SCHEMA
    import domain.settings.ops as settings_ops

    enabled = SETTINGS_SCHEMA["forum_day_report_enabled"]
    assert enabled["default"] == "off"
    assert enabled["per_city"] is True
    assert enabled["type"] == "enum"

    t = SETTINGS_SCHEMA["forum_day_report_time"]
    assert t["type"] == "text"
    assert t["format"] == "time"
    assert t["default"] == "21:00"
    assert t["per_city"] is True

    assert settings_ops.TOGGLE_SECTION["forum_day_report_enabled"] == "apps"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Хаб «🎪 Форум: функции» + экран
# ══════════════════════════════════════════════════════════════════════════════════════════

def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_hub_shows_day_report_row_and_button(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert "Отчёт дня форума" in text
    assert "forumdayreport_cfg:msk" in _cbs(kb)


def test_cfg_screen_toggle_flips_global_setting(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    callback = FakeCallback("forumdayreport_toggle:_all", ADMIN_ID)
    _run(aff.forumdayreport_toggle_go(callback))
    assert _run(db.get_setting("forum_day_report_enabled")) == "on"
    callback2 = FakeCallback("forumdayreport_toggle:_all", ADMIN_ID)
    _run(aff.forumdayreport_toggle_go(callback2))
    assert _run(db.get_setting("forum_day_report_enabled")) == "off"


def test_manual_send_now_button_reports_delivery(tmp_path, monkeypatch):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    _with_bot(monkeypatch)
    callback = FakeCallback("forumdayreport_now:_all", ADMIN_ID)
    _run(aff.forumdayreport_now_go(callback))
    assert callback.message.answers  # отчёт о доставке ушёл
    assert _run(db.forum_day_report_sent_days(None)) == set()  # ручной запуск не помечает


def test_csv_button_sends_document(tmp_path):
    from handlers import admin_forum_functions as aff

    class DocMessage(FakeMessage):
        def __init__(self):
            super().__init__()
            self.documents = []

        async def answer_document(self, document, caption=None):
            self.documents.append((document, caption))

    _ready(tmp_path)
    callback = FakeCallback("forumdayreport_csv:_all", ADMIN_ID)
    callback.message = DocMessage()
    _run(aff.forumdayreport_csv_go(callback))
    assert callback.message.documents
