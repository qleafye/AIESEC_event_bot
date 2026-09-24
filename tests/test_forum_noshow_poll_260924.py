"""Идея №23 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): опрос неявившихся
«почему не пришёл» — `services/forum_noshow_poll.py` + делегатская сторона
`handlers/forum_noshow_poll.py` + экран `handlers/admin_forum_functions.py`.

Стиль — `tests/test_checkin_volunteer_broadcast_260924.py`/`tests/test_forum_day_report_260924.py`
(реальный AsyncIOScheduler на временном jobstore, `asyncio.run`, шаблонная БД
`tests/_dbtpl.fast_init_db`)."""
from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from config import config
from database import db
import services.scheduler as sched
import services.forum_noshow_poll as fnsp
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, FakeMessage

ADMIN_ID = 924201
UID = 924210


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="forum_noshow_poll.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _set(key, value):
    await db.set_setting(key, value)


async def _add_delegate(tid, *, city=None, status="approved", season=None):
    await db.add_user({
        "telegram_id": tid, "full_name": f"Делегат {tid}", "username": "d",
        "event_city": city, "registration_date": "2026-01-01 00:00:00",
    })
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET status = ?, season = ? WHERE telegram_id = ?", (status, season, tid),
        )
        await conn.commit()


class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text, reply_markup)]

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
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


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


# ══════════════════════════════════════════════════════════════════════════════════════════
# БД: USER_PURGE_TABLES + аудитория
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_forum_noshow_poll_in_user_purge_tables():
    names = {t for t, _col, _grp in db.USER_PURGE_TABLES}
    assert "forum_noshow_poll" in names


def test_pending_ids_includes_approved_without_entry(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    assert _run(db.forum_noshow_poll_pending_ids()) == [1]


def test_pending_ids_excludes_already_checked_in(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))
    assert _run(db.forum_noshow_poll_pending_ids()) == []


def test_pending_ids_excludes_entry_on_any_day_not_just_today(tmp_path):
    """Вход каждый день: отметка входа в ЛЮБОЙ день форума (не обязательно сегодня) исключает
    делегата из опроса — «неявившийся» = нет входа НИ В ОДИН день, в отличие от отчёта дня,
    который смотрит на конкретный день."""
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="csv", scanned_at="2026-10-30 09:00:00"))
    assert _run(db.forum_noshow_poll_pending_ids()) == []


def test_pending_ids_multiple_entry_rows_do_not_duplicate_or_break_exclusion(tmp_path):
    """Несколько строк входа на одного делегата (двухдневный форум) не дают дублей в выдаче и
    не ломают NOT EXISTS — делегат с двумя отметками исключён РОВНО один раз, а не как две
    разные строки."""
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(_add_delegate(2))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="csv", scanned_at="2026-10-30 09:00:00"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="csv", scanned_at="2026-10-31 09:00:00"))
    assert _run(db.forum_noshow_poll_pending_ids()) == [2]


def test_pending_ids_excludes_not_approved(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1, status="pending"))
    assert _run(db.forum_noshow_poll_pending_ids()) == []


def test_pending_ids_excludes_past_season(tmp_path):
    _ready(tmp_path)
    _run(_set("event_season", "YL 26/2"))
    _run(_add_delegate(1, season="YL 26/1"))
    assert _run(db.forum_noshow_poll_pending_ids()) == []


def test_pending_ids_includes_current_season(tmp_path):
    _ready(tmp_path)
    _run(_set("event_season", "YL 26/2"))
    _run(_add_delegate(1, season="YL 26/2"))
    assert _run(db.forum_noshow_poll_pending_ids()) == [1]


def test_pending_ids_scoped_by_city(tmp_path):
    import cities as cities_mod
    _ready(tmp_path)
    _run(_add_delegate(1, city="msk"))
    _run(_add_delegate(2, city="spb"))
    scope_msk = cities_mod.city_scope("msk")
    assert _run(db.forum_noshow_poll_pending_ids(city_scope=scope_msk)) == [1]


def test_mark_sent_idempotent_per_season(tmp_path):
    _ready(tmp_path)
    marked = _run(db.forum_noshow_poll_mark_sent(1, "msk", "YL 26/2", "2026-10-31 12:00:00"))
    assert marked is True
    assert _run(db.forum_noshow_poll_sent_ids("YL 26/2")) == {1}
    marked2 = _run(db.forum_noshow_poll_mark_sent(1, "msk", "YL 26/2", "2026-10-31 12:05:00"))
    assert marked2 is False


def test_pending_ids_excludes_already_sent_this_season(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.forum_noshow_poll_mark_sent(1, None, "", "2026-10-31 12:00:00"))
    assert _run(db.forum_noshow_poll_pending_ids()) == []


def test_response_recorded_and_changed_by_repeated_tap(tmp_path):
    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(1, "msk", "", "2026-10-31 12:00:00"))
    ok = _run(db.record_forum_noshow_poll_response(1, "", "forgot", None, "2026-10-31 13:00:00"))
    assert ok is True
    summary = _run(db.forum_noshow_poll_summary(""))
    assert summary["by_reason"]["forgot"] == 1
    ok2 = _run(db.record_forum_noshow_poll_response(1, "", "far", None, "2026-10-31 13:05:00"))
    assert ok2 is True
    summary2 = _run(db.forum_noshow_poll_summary(""))
    assert summary2["by_reason"]["far"] == 1
    assert summary2["by_reason"]["forgot"] == 0


def test_response_to_missing_row_is_false(tmp_path):
    _ready(tmp_path)
    ok = _run(db.record_forum_noshow_poll_response(999, "", "far", None, "2026-10-31 13:00:00"))
    assert ok is False


def test_summary_counts_sent_and_answered(tmp_path):
    _ready(tmp_path)
    for tid in (1, 2, 3):
        _run(db.forum_noshow_poll_mark_sent(tid, "msk", "", "2026-10-31 12:00:00"))
    _run(db.record_forum_noshow_poll_response(1, "", "far", None, "2026-10-31 13:00:00"))
    _run(db.record_forum_noshow_poll_response(2, "", "forgot", None, "2026-10-31 13:00:00"))
    summary = _run(db.forum_noshow_poll_summary(""))
    assert summary["sent"] == 3
    assert summary["answered"] == 2
    assert summary["by_reason"] == {
        "changed_mind": 0, "study_work": 0, "far": 1, "forgot": 1, "other": 0,
    }


# ══════════════════════════════════════════════════════════════════════════════════════════
# schedule_city_job: гейты
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_disabled_by_default(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        return await fnsp.schedule_city_job(None)

    result = _run_scheduled(tmp_path, monkeypatch, body)
    assert result == {"scheduled": False, "reason": "disabled"}


def test_schedule_no_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_noshow_poll_enabled", "on"))

    async def body(s):
        return await fnsp.schedule_city_job(None)

    result = _run_scheduled(tmp_path, monkeypatch, body)
    assert result == {"scheduled": False, "reason": "no_date"}


def test_schedule_targets_day_after_last_forum_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_noshow_poll_enabled", "on"))
    _run(_set("forum_date", "30.10.2026"))
    _run(_set("sos_active_days", "2"))  # форум 30-31.10, опрос -> 01.11
    monkeypatch.setattr(fnsp, "msk_now", lambda: datetime(2026, 10, 1, 10, 0))

    async def body(s):
        result = await fnsp.schedule_city_job(None)
        assert result["scheduled"] is True
        assert result["run_at"] == datetime(2026, 11, 1, 12, 0)
        assert s.get_job(fnsp.job_id(None)) is not None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_respects_custom_time(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("forum_noshow_poll_enabled", "on"))
    _run(_set("forum_date", "30.10.2026"))
    _run(_set("sos_active_days", "1"))
    _run(_set("forum_noshow_poll_time", "09:30"))
    monkeypatch.setattr(fnsp, "msk_now", lambda: datetime(2026, 10, 1, 10, 0))

    async def body(s):
        result = await fnsp.schedule_city_job(None)
        assert result["run_at"] == datetime(2026, 10, 31, 9, 30)

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# send_poll: доставка, идемпотентность, мут, тихие часы
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_poll_delivers_with_five_buttons(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1))
    bot = _with_bot(monkeypatch)
    result = _run(fnsp.send_poll(None))
    assert result["sent"] == 1
    assert len(bot.sent) == 1
    chat_id, text, kb = bot.sent[0]
    assert chat_id == 1
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(callbacks) == 5
    assert all(c.startswith("fnsp:") for c in callbacks)
    assert {c.split(":", 1)[1] for c in callbacks} == set(fnsp.OPTION_KEYS)


def test_send_poll_idempotent_same_season(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1))
    bot = _with_bot(monkeypatch)
    _run(fnsp.send_poll(None))
    result2 = _run(fnsp.send_poll(None))
    assert result2["sent"] == 0
    assert len(bot.sent) == 1


def test_send_poll_resends_on_new_season(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1))
    bot = _with_bot(monkeypatch)
    _run(_set("event_season", "YL 26/1"))
    _run(fnsp.send_poll(None))
    _run(_set("event_season", "YL 26/2"))
    result = _run(fnsp.send_poll(None))
    assert result["sent"] == 1
    assert len(bot.sent) == 2


def test_send_poll_respects_mute(tmp_path, monkeypatch):
    from services.timeutil import msk_now
    _ready(tmp_path)
    _run(_add_delegate(1))
    today = msk_now().strftime("%Y-%m-%d")
    _run(db.set_broadcast_mute(1, today))
    bot = _with_bot(monkeypatch)
    result = _run(fnsp.send_poll(None))
    assert result == {"sent": 0, "queued": 0, "muted": 1, "failed": 0, "total": 1}
    assert bot.sent == []


def test_send_poll_queues_during_quiet_hours(tmp_path, monkeypatch):
    """В отличие от checkin_not_arrived, опрос ставится в очередь тихих часов (докстринг
    модуля) — у джобы нет ручного повтора."""
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(_set("quiet_hours_enabled", "on"))
    _run(_set("quiet_hours_start", "00:00"))
    _run(_set("quiet_hours_end", "23:59"))
    bot = _with_bot(monkeypatch)
    result = _run(fnsp.send_poll(None))
    assert result["queued"] == 1
    assert result["sent"] == 0
    assert bot.sent == []  # ничего не ушло немедленно
    assert _run(db.count_pending_delayed_notifications()) == 1


def test_send_poll_scoped_to_city(tmp_path, monkeypatch):
    import cities as cities_mod
    _ready(tmp_path)
    _run(_add_delegate(1, city="msk"))
    _run(_add_delegate(2, city="spb"))
    bot = _with_bot(monkeypatch)
    result = _run(fnsp.send_poll("msk"))
    assert result["total"] == 1
    assert bot.sent[0][0] == 1


def test_send_poll_no_bot_returns_zeroes(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1))
    monkeypatch.setattr(sched, "_bot", None)
    result = _run(fnsp.send_poll(None))
    assert result == {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers/forum_noshow_poll.py: делегатская сторона
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_fnsp_answer_records_and_thanks(tmp_path):
    from handlers import forum_noshow_poll as hfnsp
    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(UID, "msk", "", "2026-10-31 12:00:00"))
    cb = FakeCallback("fnsp:far", UID)
    state = _fresh_state(UID)
    _run(hfnsp.fnsp_answer(cb, state))
    assert cb.answers and cb.answers[-1][1] is True
    summary = _run(db.forum_noshow_poll_summary(""))
    assert summary["by_reason"]["far"] == 1


def test_fnsp_answer_change_by_repeated_tap(tmp_path):
    from handlers import forum_noshow_poll as hfnsp
    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(UID, "msk", "", "2026-10-31 12:00:00"))
    _run(hfnsp.fnsp_answer(FakeCallback("fnsp:far", UID), _fresh_state(UID)))
    _run(hfnsp.fnsp_answer(FakeCallback("fnsp:forgot", UID), _fresh_state(UID)))
    summary = _run(db.forum_noshow_poll_summary(""))
    assert summary["by_reason"]["far"] == 0
    assert summary["by_reason"]["forgot"] == 1


def test_fnsp_answer_unknown_reason_is_noop(tmp_path):
    from handlers import forum_noshow_poll as hfnsp
    _ready(tmp_path)
    cb = FakeCallback("fnsp:garbage", UID)
    _run(hfnsp.fnsp_answer(cb, _fresh_state(UID)))
    assert cb.answers == [(None, False)]


def test_fnsp_answer_stale_row_is_noop(tmp_path):
    from handlers import forum_noshow_poll as hfnsp
    _ready(tmp_path)
    cb = FakeCallback("fnsp:far", UID)  # никогда не получал опрос -- строки нет
    _run(hfnsp.fnsp_answer(cb, _fresh_state(UID)))
    assert cb.answers == [(None, False)]


def test_fnsp_answer_other_sets_state_and_prompts(tmp_path):
    from handlers import forum_noshow_poll as hfnsp
    from handlers.states import ForumNoshowPollOther

    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(UID, "msk", "", "2026-10-31 12:00:00"))
    cb = FakeCallback("fnsp:other", UID)
    state = _fresh_state(UID)
    _run(hfnsp.fnsp_answer(cb, state))
    assert cb.message.answers  # подсказка ушла
    current = _run(state.get_state())
    assert current == ForumNoshowPollOther.waiting.state


def test_fnsp_other_step_records_comment_and_thanks(tmp_path):
    from handlers import forum_noshow_poll as hfnsp

    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(UID, "msk", "", "2026-10-31 12:00:00"))
    state = _fresh_state(UID)
    msg = FakeMessage(text="Заболел(а) внезапно", user_id=UID)
    _run(hfnsp.fnsp_other_step(msg, state))
    assert msg.answers  # ответ-подтверждение ушёл
    summary = _run(db.forum_noshow_poll_summary(""))
    assert summary["by_reason"]["other"] == 1
    row = _run(db.forum_noshow_poll_summary(""))  # проверка через прямой SELECT
    async def _fetch_comment():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT comment FROM forum_noshow_poll WHERE telegram_id = ?", (UID,),
            ) as cursor:
                return (await cursor.fetchone())[0]
    assert _run(_fetch_comment()) == "Заболел(а) внезапно"


def test_fnsp_other_step_empty_text_does_not_record(tmp_path):
    from handlers import forum_noshow_poll as hfnsp

    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(UID, "msk", "", "2026-10-31 12:00:00"))
    state = _fresh_state(UID)
    msg = FakeMessage(text="   ", user_id=UID)
    _run(hfnsp.fnsp_other_step(msg, state))
    summary = _run(db.forum_noshow_poll_summary(""))
    assert summary["answered"] == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр: дефолты/формат/TOGGLE_SECTION
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_registry_defaults_and_format():
    from settings_schema import SETTINGS_SCHEMA
    import settings_ops

    enabled = SETTINGS_SCHEMA["forum_noshow_poll_enabled"]
    assert enabled["default"] == "off"
    assert enabled["per_city"] is True
    assert enabled["type"] == "enum"

    t = SETTINGS_SCHEMA["forum_noshow_poll_time"]
    assert t["type"] == "text"
    assert t["format"] == "time"
    assert t["default"] == "12:00"
    assert t["per_city"] is True

    for key in (
        "forum_noshow_poll_question_text",
        "forum_noshow_poll_option_changed_mind_text",
        "forum_noshow_poll_option_study_work_text",
        "forum_noshow_poll_option_far_text",
        "forum_noshow_poll_option_forgot_text",
        "forum_noshow_poll_option_other_text",
        "forum_noshow_poll_other_prompt_text",
        "forum_noshow_poll_thanks_text",
    ):
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text"
        assert entry["group"] == "reg"
        assert entry.get("per_city") is not True  # текст одинаков для любого города

    assert settings_ops.TOGGLE_SECTION["forum_noshow_poll_enabled"] == "apps"


def test_registry_defaults_have_manual_en_translation():
    from settings_schema import SETTINGS_SCHEMA
    from services.i18n_form_manual import FORM_DEFAULT_EN

    for key in (
        "forum_noshow_poll_question_text",
        "forum_noshow_poll_option_changed_mind_text",
        "forum_noshow_poll_option_study_work_text",
        "forum_noshow_poll_option_far_text",
        "forum_noshow_poll_option_forgot_text",
        "forum_noshow_poll_option_other_text",
        "forum_noshow_poll_other_prompt_text",
        "forum_noshow_poll_thanks_text",
    ):
        default = SETTINGS_SCHEMA[key]["default"]
        assert default in FORM_DEFAULT_EN, f"{key}: дефолт без ручного перевода EN"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Хаб «🎪 Форум: функции» + экран
# ══════════════════════════════════════════════════════════════════════════════════════════

def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_hub_shows_noshow_poll_row_and_button(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert "Опрос неявившихся" in text
    assert "forumnoshowpoll_cfg:msk" in _cbs(kb)


def test_cfg_screen_toggle_flips_global_setting(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    callback = FakeCallback("forumnoshowpoll_toggle:_all", ADMIN_ID)
    _run(aff.forumnoshowpoll_toggle_go(callback))
    assert _run(db.get_setting("forum_noshow_poll_enabled")) == "on"
    callback2 = FakeCallback("forumnoshowpoll_toggle:_all", ADMIN_ID)
    _run(aff.forumnoshowpoll_toggle_go(callback2))
    assert _run(db.get_setting("forum_noshow_poll_enabled")) == "off"


def test_cfg_screen_shows_summary_line(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    _run(db.forum_noshow_poll_mark_sent(1, None, "", "2026-10-31 12:00:00"))
    _run(db.record_forum_noshow_poll_response(1, "", "far", None, "2026-10-31 13:00:00"))
    text, _kb = _run(aff._noshow_poll_cfg_text_kb(None))
    assert "Ответили 1 из 1" in text
