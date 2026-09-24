"""Напоминания взявшему SOS — лесенка вместо бесконечного репитера (стенд 25.09: 16 напоминаний
за ночь по одной заявке, взятой накануне).

Лесенка `services.sos.CLAIMED_REMIND_DELAYS_MINUTES` (20 / 60 / 180), максимум три
напоминания, после третьего — одно сообщение менеджерам и больше ничего; тихие часы переносят
ступень на конец окна; после дней форума города — тишина; решённая/переназначенная заявка —
тишина; счётчик в БД переживает рестарт.

Время — подменённый `services.scheduler._now_moscow_naive`; постановка джобы — подменённый
`services.sos._schedule_claimed_reminder_at` (планировщик в тестах не поднят).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import services.scheduler as scheduler_module
from database import db
from services import sos as sos_service
from tests.test_sos_260924 import ADMIN_ID, DELEGATE_ID, MANAGER_ID, FakeBot, _ready, _run

FORUM_DAY = datetime(2026, 10, 30, 12, 0)
ESCALATION_MARK = "три напоминания"


@pytest.fixture
def env(tmp_path, monkeypatch):
    _ready(tmp_path, "test_sos_claimed_ladder.db")
    _run(db.set_setting("forum_date", FORUM_DAY.strftime("%d.%m.%Y")))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    bot = FakeBot()
    monkeypatch.setattr(scheduler_module, "get_bot", lambda: bot)
    clock = {"now": FORUM_DAY}
    monkeypatch.setattr(scheduler_module, "_now_moscow_naive", lambda: clock["now"])
    scheduled: list[tuple[int, int | None, datetime]] = []
    monkeypatch.setattr(
        sos_service, "_schedule_claimed_reminder_at",
        lambda report_id, claimant_id, run_at: scheduled.append((report_id, claimant_id, run_at)),
    )
    return {"bot": bot, "clock": clock, "scheduled": scheduled}


def _claimed_report(claimed_at: datetime) -> int:
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.claim_sos_report(rid, ADMIN_ID, "Админ Первый"))

    async def _stamp():
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE sos_reports SET claimed_at = ? WHERE id = ?",
                (claimed_at.strftime("%Y-%m-%d %H:%M:%S"), rid),
            )
            await conn.commit()

    _run(_stamp())
    return rid


def _reminders(bot) -> list[str]:
    return [text for chat_id, text, _ in bot.sent if chat_id == ADMIN_ID and "у тебя в работе" in text]


def _escalations(bot) -> list[str]:
    return [text for _, text, _ in bot.sent if ESCALATION_MARK in text]


def _count(rid: int) -> int:
    return _run(db.get_sos_report(rid))["claimed_remind_count"]


def test_ladder_three_reminders_then_one_escalation_then_silence(env):
    bot, clock, scheduled = env["bot"], env["clock"], env["scheduled"]
    claimed_at = FORUM_DAY - timedelta(minutes=20)
    rid = _claimed_report(claimed_at)

    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert len(_reminders(bot)) == 1
    assert "20 мин" in _reminders(bot)[0]
    assert scheduled[-1] == (rid, ADMIN_ID, clock["now"] + timedelta(minutes=60))

    clock["now"] = scheduled[-1][2]
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert len(_reminders(bot)) == 2
    assert scheduled[-1] == (rid, ADMIN_ID, clock["now"] + timedelta(minutes=180))

    clock["now"] = scheduled[-1][2]
    n_scheduled = len(scheduled)
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert len(_reminders(bot)) == 3
    assert len(scheduled) == n_scheduled  # после третьего новой ступени нет
    escalations = _escalations(bot)
    assert escalations, "после третьего напоминания менеджеры должны получить сообщение"
    assert any(chat_id == MANAGER_ID for chat_id, text, _ in bot.sent if ESCALATION_MARK in text)
    assert "Админ Первый" in escalations[0]
    assert _count(rid) == sos_service.CLAIMED_REMIND_MAX

    sent_before = len(bot.sent)
    clock["now"] += timedelta(hours=5)
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert len(bot.sent) == sent_before
    assert len(scheduled) == n_scheduled


def test_quiet_hours_postpone_to_window_end_without_losing_step(env):
    bot, clock, scheduled = env["bot"], env["clock"], env["scheduled"]
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "22:00"))
    _run(db.set_setting("quiet_hours_end", "09:00"))
    clock["now"] = FORUM_DAY.replace(hour=23, minute=30)
    rid = _claimed_report(clock["now"] - timedelta(minutes=20))

    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))

    assert bot.sent == []
    assert _count(rid) == 0  # ступень не израсходована
    assert scheduled == [(rid, ADMIN_ID, datetime(2026, 10, 31, 9, 0))]

    clock["now"] = datetime(2026, 10, 31, 9, 0)
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert len(_reminders(bot)) == 1
    assert _count(rid) == 1


def test_quiet_hours_after_last_forum_day_drop_the_step(env):
    """Конец тихого окна уже после последнего дня форума — переносить некуда."""
    bot, clock, scheduled = env["bot"], env["clock"], env["scheduled"]
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "22:00"))
    _run(db.set_setting("quiet_hours_end", "09:00"))
    clock["now"] = datetime(2026, 10, 31, 23, 0)  # вечер последнего (второго) дня
    rid = _claimed_report(clock["now"] - timedelta(minutes=20))
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert bot.sent == []
    assert scheduled == []


def test_outside_forum_window_nothing_sent_nothing_scheduled(env):
    bot, clock, scheduled = env["bot"], env["clock"], env["scheduled"]
    clock["now"] = FORUM_DAY + timedelta(days=3)
    rid = _claimed_report(clock["now"] - timedelta(minutes=20))
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert bot.sent == []
    assert scheduled == []
    assert _count(rid) == 0


def test_next_step_not_scheduled_past_forum_end(env):
    bot, clock, scheduled = env["bot"], env["clock"], env["scheduled"]
    clock["now"] = datetime(2026, 10, 31, 23, 30)  # следующая ступень (+60 мин) — уже 1 ноября
    rid = _claimed_report(clock["now"] - timedelta(minutes=20))
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert len(_reminders(bot)) == 1
    assert scheduled == []


def test_resolved_report_nothing(env):
    bot, scheduled = env["bot"], env["scheduled"]
    rid = _claimed_report(FORUM_DAY - timedelta(minutes=20))
    _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ Первый"))
    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))
    assert bot.sent == []
    assert scheduled == []


def test_other_claimant_job_is_silent(env):
    bot, scheduled = env["bot"], env["scheduled"]
    rid = _claimed_report(FORUM_DAY - timedelta(minutes=20))
    _run(sos_service.claimed_reminder_job(rid, claimant_id=MANAGER_ID))
    assert bot.sent == []
    assert scheduled == []


def test_restart_keeps_counter(env):
    """Счётчик в БД: после двух напоминаний рестарт (свежий вызов джобы из хранилища) даёт
    третье напоминание + сообщение менеджерам, а не начинает лесенку заново."""
    bot, scheduled = env["bot"], env["scheduled"]
    rid = _claimed_report(FORUM_DAY - timedelta(minutes=260))
    assert _run(db.advance_sos_claimed_remind(rid, ADMIN_ID, 0))
    assert _run(db.advance_sos_claimed_remind(rid, ADMIN_ID, 1))

    _run(sos_service.claimed_reminder_job(rid, claimant_id=ADMIN_ID))

    assert len(_reminders(bot)) == 1
    assert _escalations(bot)
    assert scheduled == []
    assert _count(rid) == 3


def test_same_step_fired_twice_sends_once(env):
    """compare-and-set: ступень, которую уже сдвинули, повторно не шлётся."""
    rid = _claimed_report(FORUM_DAY - timedelta(minutes=20))
    assert _run(db.advance_sos_claimed_remind(rid, ADMIN_ID, 0))
    assert not _run(db.advance_sos_claimed_remind(rid, ADMIN_ID, 0))


def test_legacy_job_args_still_work(env):
    """Джобы, поставленные до лесенки, лежат в хранилище с аргументами (report_id, minutes)."""
    bot, scheduled = env["bot"], env["scheduled"]
    rid = _claimed_report(FORUM_DAY - timedelta(minutes=20))
    _run(sos_service.claimed_reminder_job(rid, 20))
    assert len(_reminders(bot)) == 1
    assert scheduled[-1][2] == FORUM_DAY + timedelta(minutes=60)
