"""D-33 (решение владельца 24.09, `.planning/FORUM-CHECKIN.md`): шпаргалка волонтёра чек-ина
за день до форума ВСЕМ держателям capability `checkin` города
(`services/checkin_volunteer_broadcast.py`).

Стиль — `tests/test_checkin_qr_broadcast_260924.py` (реальный AsyncIOScheduler на временном
jobstore, `asyncio.run`, шаблонная БД `tests/_dbtpl.fast_init_db`)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from handlers.access.admin_caps import role_caps_key, role_enabled_key
import services.scheduler as sched
import services.checkin_volunteer_broadcast as vb
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260924301
VOLUNTEER_ID = 260924302
VOLUNTEER2_ID = 260924303


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="checkin_volunteer_broadcast.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    # Шпаргалка идёт только там, где включён «🎟 Вход по QR» (мастер чек-ина).
    _run(db.set_setting("checkin_qr_enabled", "on"))


async def _grant_checkin(tid, city=None):
    await db.set_setting(role_enabled_key("reg_manager"), "on")
    await db.set_setting(role_caps_key("reg_manager"), "checkin")
    await db.add_staff(tid, "reg_manager", SUPERADMIN_ID)
    if city:
        await db.set_staff_city(tid, city)


async def _set_setting(key, value):
    await db.set_setting(key, value)


class FakeBot:
    def __init__(self):
        self.messages = []  # [(chat_id, text)]

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
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
# Чистый хелпер: день накануне форума, заданное время
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_guide_run_at_is_day_before_at_given_time():
    dt = vb.guide_run_at("03.10.2026", "17:00")
    assert dt == datetime(2026, 10, 2, 17, 0)


def test_guide_run_at_none_without_forum_date():
    assert vb.guide_run_at(None, "17:00") is None


def test_guide_run_at_none_on_unparseable_date():
    assert vb.guide_run_at("не дата", "17:00") is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# schedule_city_job: гейты (дата форума / тумблер / пустой текст)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_skipped_without_forum_date(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "no_date"}
        assert s.get_job(vb.job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_skipped_when_disabled(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_broadcast_enabled", "off"))
    _run(_set_setting("checkin_volunteer_guide_text", "Шпаргалка"))

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "disabled"}

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_skipped_when_text_empty(tmp_path, monkeypatch):
    """Пустой текст шпаргалки (менеджер стёр дефолт) гасит джобу — отправлять пустое сообщение
    персоналу нет смысла."""
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", ""))

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "empty_text"}

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_creates_job_with_forum_date_and_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result["scheduled"] is True
        assert result["run_at"] == datetime(2026, 10, 2, 17, 0)
        job = s.get_job(vb.job_id(None))
        assert job is not None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_respects_custom_time(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    _run(_set_setting("checkin_volunteer_guide_broadcast_time", "12:30"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result["run_at"] == datetime(2026, 10, 2, 12, 30)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_clears_job_when_forum_date_removed(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        await vb.schedule_city_job(None)
        assert s.get_job(vb.job_id(None)) is not None
        await db.delete_setting("forum_date")
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "no_date"}
        assert s.get_job(vb.job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# send_guide: аудитория, идемпотентность по дню форума
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_guide_sends_to_capability_holders(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Как отмечать делегатов"))
    _run(_grant_checkin(VOLUNTEER_ID))
    bot = _with_bot(monkeypatch)

    # capability_holders включает и волонтёра, и суперадмина (тот держит ЛЮБУЮ capability
    # бутстрапом, D-12) — тест смотрит на присутствие волонтёра в получателях, не на
    # исключительность (сравнение с суперадмином — забота capability_holders, не эта задача).
    result = _run(vb.send_guide(None))
    assert result["sent"] == 2
    recipient_ids = {cid for cid, _text in bot.messages}
    assert VOLUNTEER_ID in recipient_ids
    assert all(text == "🎫 Как отмечать делегатов" for _cid, text in bot.messages)


def test_send_guide_idempotent_same_forum_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    _run(_grant_checkin(VOLUNTEER_ID))
    bot = _with_bot(monkeypatch)

    result1 = _run(vb.send_guide(None))
    assert result1["sent"] == 2  # волонтёр + суперадмин (см. комментарий выше)
    first_count = len(bot.messages)
    result2 = _run(vb.send_guide(None))
    assert result2["sent"] == 0
    assert len(bot.messages) == first_count  # не задублировалось


def test_send_guide_resends_on_different_forum_day(tmp_path, monkeypatch):
    """D-33: идемпотентность ПО ДНЮ форума, не по человеку раз и навсегда — тот же волонтёр
    на форуме СЛЕДУЮЩЕЙ даты получает напоминание заново."""
    _ready(tmp_path)
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    _run(_grant_checkin(VOLUNTEER_ID))
    bot = _with_bot(monkeypatch)

    _run(_set_setting("forum_date", "03.10.2026"))
    _run(vb.send_guide(None))
    first_count = len(bot.messages)
    _run(_set_setting("forum_date", "31.10.2026"))
    result = _run(vb.send_guide(None))
    assert result["sent"] == first_count
    assert len(bot.messages) == first_count * 2
    assert bot.messages.count((VOLUNTEER_ID, "🎫 Шпаргалка")) == 2


def test_send_guide_empty_text_skips(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", ""))
    _run(_grant_checkin(VOLUNTEER_ID))
    bot = _with_bot(monkeypatch)

    result = _run(vb.send_guide(None))
    assert result == {"sent": 0, "failed": 0, "total": 0, "skipped": "empty_text"}
    assert bot.messages == []


def test_send_guide_scoped_to_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(_set_setting("forum_date__city__spb", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    _run(_grant_checkin(VOLUNTEER_ID, city="spb"))
    _run(_grant_checkin(VOLUNTEER2_ID, city="tyumen"))
    bot = _with_bot(monkeypatch)

    # Суперадмин привязки не держит (D-12: всегда в holders) -- + spb-волонтёр = 2; tyumen-
    # волонтёр в scope "spb" не адресуется.
    result = _run(vb.send_guide("spb"))
    assert result["sent"] == 2
    recipient_ids = {cid for cid, _text in bot.messages}
    assert VOLUNTEER_ID in recipient_ids
    assert VOLUNTEER2_ID not in recipient_ids


# ══════════════════════════════════════════════════════════════════════════════════════════
# reconcile: по каждому включённому городу (или один общий проход, модуль выключен)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reconcile_schedules_each_enabled_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(_set_setting("forum_date__city__spb", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        touched = await vb.reconcile()
        assert "spb" in touched
        assert s.get_job(vb.job_id("spb")) is not None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_reconcile_module_off_uses_single_pass(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        touched = await vb.reconcile()
        assert touched == [None]
        assert s.get_job(vb.job_id(None)) is not None

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Гейт чек-ина и прошедшей даты: шпаргалка не уходит там, где «🎟 Вход по QR» выключен,
# и не догоняется после начала форума
# ══════════════════════════════════════════════════════════════════════════════════════════

def _guide_setup(tmp_path):
    _ready(tmp_path)
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))


def test_schedule_skipped_when_checkin_master_off(tmp_path, monkeypatch):
    """forum_date задана (её читают правила автоотказа), тумблер шпаргалки по умолчанию вкл,
    текст непустой — но чек-ин не используется: джобы нет."""
    _guide_setup(tmp_path)
    _run(_set_setting("checkin_qr_enabled", "off"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "disabled"}
        assert s.get_job(vb.job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_run_job_skips_when_checkin_master_turned_off(tmp_path, monkeypatch):
    _guide_setup(tmp_path)
    _run(_grant_checkin(VOLUNTEER_ID))
    _run(_set_setting("checkin_qr_enabled", "off"))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 10, 2, 17, 0))
    bot = _with_bot(monkeypatch)

    result = _run(vb._run_job(None))
    assert result["sent"] == 0
    assert bot.messages == []


def test_schedule_past_forum_date_cancels(tmp_path, monkeypatch):
    _guide_setup(tmp_path)
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        await vb.schedule_city_job(None)
        assert s.get_job(vb.job_id(None)) is not None
        monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 10, 4, 10, 0))
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "past"}
        assert s.get_job(vb.job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_forum_today_does_not_catch_up(tmp_path, monkeypatch):
    """День форума, утренний слот (08:00) уже прошёл — «завтра форум» не шлём."""
    _guide_setup(tmp_path)
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 10, 3, 12, 0))

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result == {"scheduled": False, "reason": "too_late"}
        assert s.get_job(vb.job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_late_enable_day_before_catches_up(tmp_path, monkeypatch):
    _guide_setup(tmp_path)
    now = datetime(2026, 10, 2, 20, 0)
    monkeypatch.setattr(vb, "msk_now", lambda: now)

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result["run_at"] == now + timedelta(minutes=1)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_run_job_wrong_day_skips_without_marking_sent(tmp_path, monkeypatch):
    """Дата форума сменилась из Mini App: джоба старого кануна не шлёт «завтра форум» и не
    пишет отметку под новый день (иначе настоящий канун её бы пропустил)."""
    import services.checkin_broadcast as cb

    _guide_setup(tmp_path)
    _run(_grant_checkin(VOLUNTEER_ID))
    now = lambda: datetime(2026, 10, 2, 17, 0)
    monkeypatch.setattr(vb, "msk_now", now)
    monkeypatch.setattr(cb, "msk_now", now)
    bot = _with_bot(monkeypatch)

    async def body(s):
        await _set_setting("forum_date", "10.10.2026")
        result = await vb._run_job(None)
        assert result.get("skipped") == "wrong_day"
        assert bot.messages == []
        assert await db.checkin_volunteer_guide_sent_ids("2026-10-10") == set()
        job = s.get_job(vb.job_id(None))
        assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 9, 17, 0)

        await _set_setting("forum_date", "03.10.2026")
        result = await vb._run_job(None)
        assert result["sent"] >= 1

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_eve_2159_catches_up(tmp_path, monkeypatch):
    _guide_setup(tmp_path)
    now = datetime(2026, 10, 2, 21, 59)
    monkeypatch.setattr(vb, "msk_now", lambda: now)

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result["run_at"] == now + timedelta(minutes=1)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_eve_2201_moves_to_forum_morning(tmp_path, monkeypatch):
    """После 22:00 кануна шпаргалка не уходит ночью — ставится на утро форума, в время
    утреннего повтора QR, и на срабатывании (день форума) уходит."""
    import services.checkin_broadcast as cb

    _guide_setup(tmp_path)
    _run(_set_setting("checkin_qr_morning_repeat_time", "08:00"))
    _run(_grant_checkin(VOLUNTEER_ID))
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2026, 10, 2, 22, 1))
    bot = _with_bot(monkeypatch)

    async def body(s):
        result = await vb.schedule_city_job(None)
        assert result["run_at"] == datetime(2026, 10, 3, 8, 0)
        job = s.get_job(vb.job_id(None))
        assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 3, 8, 0)

        morning = lambda: datetime(2026, 10, 3, 8, 0)
        monkeypatch.setattr(vb, "msk_now", morning)
        monkeypatch.setattr(cb, "msk_now", morning)
        sent = await vb._run_job(None)
        assert sent["sent"] >= 1
        assert VOLUNTEER_ID in {cid for cid, _t in bot.messages}
        again = await vb._run_job(None)  # отметка по дню форума — без дублей
        assert again["sent"] == 0

    _run_scheduled(tmp_path, monkeypatch, body)
