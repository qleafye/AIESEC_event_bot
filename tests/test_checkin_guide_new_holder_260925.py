"""Шпаргалка волонтёра тому, кто получил право checkin ПОСЛЕ рассылки накануне
(`services/checkin_volunteer_broadcast.py::greet_new_holder`): выдали роль или право
существующей роли в канун/день форума — шпаргалка уходит сразу, с отметкой в
`checkin_volunteer_guide_sends` (джоба и повторная выдача второй раз не шлют); канун после
22:00 — утренней джобой; до рассылки накануне и вне гейтов — прежняя отправка при назначении
(B3) без отметки."""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db
from handlers import admin_roles
from handlers.admin_caps import role_caps_key, role_enabled_key
import services.scheduler as sched
import services.checkin_volunteer_broadcast as vb
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260925401
VOL1 = 260925402
VOL2 = 260925403
ROLE = "game_manager"  # дефолтные caps без checkin
DAY = "2026-10-03"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, monkeypatch, now):
    config.DB_PATH = str(tmp_path / "guide_new_holder.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    _run(db.set_setting(role_enabled_key(ROLE), "on"))
    monkeypatch.setattr(vb, "msk_now", lambda: now)
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    async def edit_text(self, *a, **k):
        return None


class _Cb:
    def __init__(self, data, bot):
        self.data = data
        self.from_user = _User(SUPERADMIN_ID)
        self.message = _Msg()
        self.bot = bot

    async def answer(self, *a, **k):
        return None


def _assign(bot, tid):
    _run(admin_roles.roles_assign(_Cb(f"roles_addrole:{tid}:{ROLE}", bot), bot))


def _sent_ids():
    return _run(db.checkin_volunteer_guide_sent_ids(DAY))


def test_eve_after_broadcast_sends_now_with_mark_and_job_skips_him(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, datetime(2026, 10, 2, 19, 0))
    _run(db.set_setting(role_caps_key(ROLE), "checkin"))
    _assign(bot, VOL1)
    assert bot.sent == [(VOL1, "🎫 Шпаргалка")]
    assert VOL1 in _sent_ids()
    bot.sent.clear()
    _run(vb.send_guide(None))  # утренний повтор джобы — ему второй раз не уходит
    assert VOL1 not in {cid for cid, _ in bot.sent}


def test_forum_day_grant_to_existing_role_sends_each_new_holder_once(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, datetime(2026, 10, 3, 9, 30))
    _run(db.add_staff(VOL1, ROLE, SUPERADMIN_ID))
    _run(db.add_staff(VOL2, ROLE, SUPERADMIN_ID))
    cb = _Cb(f"roles_cap:{ROLE}:checkin", bot)
    _run(admin_roles.toggle_role_cap(cb, bot))
    assert sorted(cid for cid, _ in bot.sent) == [VOL1, VOL2]
    assert {VOL1, VOL2} <= _sent_ids()
    # сняли и снова выдали — в тот же день форума без дублей
    _run(admin_roles.toggle_role_cap(cb, bot))
    _run(admin_roles.toggle_role_cap(cb, bot))
    assert len(bot.sent) == 2


def test_enabling_role_with_checkin_greets_holders(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, datetime(2026, 10, 3, 9, 30))
    _run(db.set_setting(role_caps_key(ROLE), "checkin"))
    _run(db.set_setting(role_enabled_key(ROLE), "off"))
    _run(db.add_staff(VOL1, ROLE, SUPERADMIN_ID))
    _run(admin_roles.toggle_role_enabled(_Cb(f"roles_toggle:{ROLE}", bot), bot))
    assert bot.sent == [(VOL1, "🎫 Шпаргалка")]


def test_eve_after_22_goes_to_morning_job(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, datetime(2026, 10, 2, 23, 10))
    _run(db.set_setting(role_caps_key(ROLE), "checkin"))
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    s = AsyncIOScheduler(jobstores={"default": MemoryJobStore()}, timezone=sched.MOSCOW_TZ)
    monkeypatch.setattr(sched, "_scheduler", s)

    async def go():
        s.start(paused=True)
        try:
            await admin_roles.roles_assign(_Cb(f"roles_addrole:{VOL1}:{ROLE}", bot), bot)
            return s.get_job(vb.job_id(None))
        finally:
            s.shutdown(wait=False)

    job = asyncio.run(go())
    assert bot.sent == []  # ночью не будим
    assert job is not None and job.next_run_time.date().isoformat() == DAY


def test_before_eve_broadcast_keeps_plain_send_without_mark(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, datetime(2026, 9, 25, 12, 0))
    _run(db.set_setting(role_caps_key(ROLE), "checkin"))
    _assign(bot, VOL1)
    assert bot.sent == [(VOL1, "🎫 Шпаргалка")]
    assert _sent_ids() == set()  # накануне рассылка напомнит ещё раз — это другой повод


def test_checkin_master_off_no_mark(tmp_path, monkeypatch):
    bot = _ready(tmp_path, monkeypatch, datetime(2026, 10, 2, 19, 0))
    _run(db.set_setting("checkin_qr_enabled", "off"))
    assert _run(vb.guide_for_new_holder(VOL1)) == "skipped"


def test_forum_passed_is_skipped(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, datetime(2026, 10, 4, 10, 0))
    _run(db.set_setting(role_caps_key(ROLE), "checkin"))
    _run(db.add_staff(VOL1, ROLE, SUPERADMIN_ID))
    assert _run(vb.guide_for_new_holder(VOL1)) == "skipped"
    assert _sent_ids() == set()
