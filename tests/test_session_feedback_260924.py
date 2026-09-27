"""Форум-ночь п.9 (идея №15 бэклога чек-ина, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`; D-24
`.planning/FORUM-CHECKIN.md`): «⭐ Отзыв о сессии одним тапом» — БД (`session_feedback`),
домен (`services/session_feedback.py`) и делегатский шов (`handlers/session_feedback.py`).

pytest-asyncio недоступен (см. `tests/test_db_phase5.py`) — каждый async-вызов через
`asyncio.run()`, `config.DB_PATH` смотрит в `tmp_path`, БД — шаблонная копия
`tests/_dbtpl.py::fast_init_db`. Планирование джоб — реальный `AsyncIOScheduler` на временном
jobstore (тот же приём, что `tests/test_ambassador_wave_scheduling_32.py::_run_scheduled`).
Fake-объекты делегатского шва — форма `tests/test_sos_260924.py` (FakeUser/FakeChat/
FakeMessage/FakeCallback/FakeBot)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import services.scheduler as sched
import services.session_feedback as sf
import handlers.session_feedback as sf_handlers
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

DELEGATE_ID = 903010
DELEGATE2_ID = 903011
MANAGER_ID = 903001


def _ready(tmp_path, name="test_session_feedback_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _add_delegate(tid: int, *, full_name="Тест Делегатов"):
    await db.add_user({
        "telegram_id": tid, "full_name": full_name,
        "registration_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "approved",
    })


def _TOMORROW_NOON():
    """Завтра 12:00 по Москве — «будущая» сессия, у которой ЧЧ:ММ начала и конца не переходят
    через полночь ни при каком времени прогона."""
    return (msk_now() + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)


async def _make_session(city="msk", *, day=None, start_offset_min=-60, end_offset_min=-30):
    """Сессия, чьи `day`/`start_time`/`end_time` считаются от РЕАЛЬНОГО `msk_now()` — по
    умолчанию уже закончилась 30 минут назад (типичный сценарий джобы отзыва). `day` берётся у
    `end` (не у "сейчас") — большой отрицательный `end_offset_min` (тест «далеко в прошлом»)
    может перевалить за полночь, календарный день сессии обязан сдвинуться вместе с ним."""
    now = msk_now()
    start = now + timedelta(minutes=start_offset_min)
    end = now + timedelta(minutes=end_offset_min)
    return await db.create_program_session(
        city, day or end.strftime("%Y-%m-%d"), start.strftime("%H:%M"), end.strftime("%H:%M"),
        "Тестовая сессия",
    )


async def _mark_prompt_and_set(tid: int, sid: int, *, rating: int | None = None, comment: str | None = None):
    """Полный путь делегата ДО оценки/комментария: отметка на сессии + строка-приглашение
    (`create_session_feedback_prompt`) — `record_rating`/`record_comment` это UPDATE по уже
    существующей строке, не upsert (см. докстринги обеих функций), тот же контур, что
    `deliver_feedback_prompts` строит в проде."""
    await db.record_session_checkin(tid, sid, [], source="miniapp")
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    await db.create_session_feedback_prompt(tid, sid, stamp)
    if rating is not None:
        await sf.record_rating(tid, sid, rating)
    if comment is not None:
        await sf.record_comment(tid, sid, comment)


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, text=None, user_id=None):
        self.text = text
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(user_id)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edit_kwargs = kwargs


class FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


def _with_bot(monkeypatch, bot=None):
    bot = bot or FakeBot()
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
# D-24: только отмеченные (checkins.point == "session:{id}")
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_is_marked_for_session_true_only_for_checked_in_delegate(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    assert _run(sf.is_marked_for_session(DELEGATE_ID, sid)) is True
    assert _run(sf.is_marked_for_session(DELEGATE2_ID, sid)) is False


def test_list_marked_telegram_ids_for_session(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    other_sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    _run(db.record_session_checkin(DELEGATE2_ID, other_sid, [], source="miniapp"))
    assert _run(sf.list_marked_telegram_ids_for_session(sid)) == [DELEGATE_ID]


def test_d20_delegate_moved_to_another_session_in_slot_marked_only_for_final(tmp_path):
    """D-20: параллельные сессии одного слота — засчитывается ПОСЛЕДНИЙ скан, делегат,
    ушедший с первой на вторую, оценивает только вторую."""
    _ready(tmp_path)
    sid_a = _run(_make_session())
    sid_b = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid_a, [sid_b], source="miniapp"))
    status, _stamp, _prev = _run(
        db.record_session_checkin(DELEGATE_ID, sid_b, [sid_a], source="miniapp"),
    )
    assert status == "moved"
    assert _run(sf.is_marked_for_session(DELEGATE_ID, sid_a)) is False
    assert _run(sf.is_marked_for_session(DELEGATE_ID, sid_b)) is True


# ══════════════════════════════════════════════════════════════════════════════════════════
# Идемпотентность рассылки + повторный тап оценки + комментарий
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_create_session_feedback_prompt_idempotent(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    assert _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp)) is True
    assert _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp)) is False  # уже приглашали


def test_record_rating_rejects_unmarked_delegate(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    assert _run(sf.record_rating(DELEGATE_ID, sid, 5)) is False


def test_record_rating_accepts_marked_delegate_and_retap_changes_value(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp))

    assert _run(sf.record_rating(DELEGATE_ID, sid, 3)) is True
    row = _run(db.get_session_feedback(DELEGATE_ID, sid))
    assert row["rating"] == 3

    assert _run(sf.record_rating(DELEGATE_ID, sid, 5)) is True  # повторный тап меняет оценку
    row = _run(db.get_session_feedback(DELEGATE_ID, sid))
    assert row["rating"] == 5


def test_record_rating_rejects_out_of_range(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    assert _run(sf.record_rating(DELEGATE_ID, sid, 0)) is False
    assert _run(sf.record_rating(DELEGATE_ID, sid, 6)) is False


def test_record_comment_rejects_unmarked_delegate(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    assert _run(sf.record_comment(DELEGATE_ID, sid, "Отлично!")) is False


def test_record_comment_saved_for_marked_delegate(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp))
    assert _run(sf.record_comment(DELEGATE_ID, sid, "  Отлично!  ")) is True
    row = _run(db.get_session_feedback(DELEGATE_ID, sid))
    assert row["comment"] == "Отлично!"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Статистика
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_session_feedback_stats_no_ratings_returns_none_avg(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    stats = _run(sf.session_feedback_stats(sid))
    assert stats == {"avg": None, "rating_count": 0, "comment_count": 0}
    assert sf.stats_line(stats) == "Пока нет оценок"


def test_session_feedback_stats_with_ratings_and_comments(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    for tid, rating, comment in ((DELEGATE_ID, 5, "Супер"), (DELEGATE2_ID, 3, None)):
        _run(db.record_session_checkin(tid, sid, [], source="miniapp"))
        stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
        _run(db.create_session_feedback_prompt(tid, sid, stamp))
        _run(sf.record_rating(tid, sid, rating))
        if comment:
            _run(sf.record_comment(tid, sid, comment))
    stats = _run(sf.session_feedback_stats(sid))
    assert stats["rating_count"] == 2
    assert stats["comment_count"] == 1
    assert abs(stats["avg"] - 4.0) < 1e-6
    assert "⭐ 4.0 (2 оценки)" in sf.stats_line(stats)
    assert "1 комментарий" in sf.stats_line(stats)


def test_session_feedback_stats_bulk_matches_single(tmp_path):
    _ready(tmp_path)
    sid1 = _run(_make_session())
    sid2 = _run(_make_session())
    _run(_mark_prompt_and_set(DELEGATE_ID, sid1, rating=4))
    bulk = _run(db.session_feedback_stats_bulk([sid1, sid2]))
    assert bulk[sid1]["rating_count"] == 1
    assert sid2 not in bulk  # сессия без оценок отсутствует, вызывающий подставляет ноль сам


def test_list_session_feedback_comments_pagination(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    for i in range(3):
        tid = DELEGATE_ID + i
        _run(_add_delegate(tid))
        _run(_mark_prompt_and_set(tid, sid, rating=4, comment=f"Комментарий {i}"))
    rows, total = _run(sf.comments_page(sid, page=0, page_size=2))
    assert total == 3
    assert len(rows) == 2
    rows2, _total2 = _run(sf.comments_page(sid, page=1, page_size=2))
    assert len(rows2) == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Планирование джоб + перестановка при правке (реальный AsyncIOScheduler)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_for_session_uses_end_time_plus_delay(tmp_path, monkeypatch):
    _ready(tmp_path)
    # Завтра в 12:00–12:30, а не «сейчас + 30 мин»: около полуночи «ЧЧ:ММ» конца переваливал
    # за 00:00 и оказывался раньше начала — на CI (прогон в ~22:00 МСК) тест падал.
    now = _TOMORROW_NOON()
    end = now + timedelta(minutes=30)
    sid = _run(db.create_program_session(
        "msk", now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), end.strftime("%H:%M"), "Сессия",
    ))
    _run(db.set_setting("session_feedback_delay_minutes", "10"))

    async def body(s):
        ok = await sf.schedule_for_session(sid)
        assert ok is True
        job = s.get_job(sf.feedback_job_id(sid))
        assert job is not None
        expected = (end + timedelta(minutes=10)).replace(second=0, microsecond=0)
        actual = job.trigger.run_date.replace(tzinfo=None).replace(second=0, microsecond=0)
        assert actual == expected

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_for_session_reschedule_on_edit_replaces_not_duplicates(tmp_path, monkeypatch):
    _ready(tmp_path)
    now = _TOMORROW_NOON()  # см. test_schedule_for_session_uses_end_time_plus_delay — полночь
    sid = _run(db.create_program_session(
        "msk", now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
        (now + timedelta(minutes=30)).strftime("%H:%M"), "Сессия",
    ))

    async def body(s):
        await sf.schedule_for_session(sid)
        first_run = s.get_job(sf.feedback_job_id(sid)).trigger.run_date

        # Правка времени конца — перепланирование должно ПЕРЕСТАВИТЬ ту же джобу, не завести
        # вторую (тот же id, replace_existing=True).
        new_end = now + timedelta(hours=2)
        await db.update_program_session(sid, end_time=new_end.strftime("%H:%M"))
        await sf.schedule_for_session(sid)

        jobs = [j for j in s.get_jobs() if j.id == sf.feedback_job_id(sid)]
        assert len(jobs) == 1
        assert jobs[0].trigger.run_date != first_run

    _run_scheduled(tmp_path, monkeypatch, body)


def test_cancel_for_session_removes_job(tmp_path, monkeypatch):
    _ready(tmp_path)
    sid = _run(_make_session(start_offset_min=30, end_offset_min=60))

    async def body(s):
        await sf.schedule_for_session(sid)
        assert s.get_job(sf.feedback_job_id(sid)) is not None
        sf.cancel_for_session(sid)
        assert s.get_job(sf.feedback_job_id(sid)) is None
        sf.cancel_for_session(sid)  # повторная отмена — fail-soft, не падает

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_for_session_skips_when_far_in_past(tmp_path, monkeypatch):
    """Сессия закончилась намного раньше окна `_CATCHUP_GRACE_HOURS` назад — джоба НЕ
    ставится (форум мог смениться днём, догонять поздно)."""
    _ready(tmp_path)
    sid = _run(_make_session(start_offset_min=-2000, end_offset_min=-1980))

    async def body(s):
        ok = await sf.schedule_for_session(sid)
        assert ok is False
        assert s.get_job(sf.feedback_job_id(sid)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_for_session_catches_up_when_recently_past(tmp_path, monkeypatch):
    """Сессия закончилась недавно (в пределах `_CATCHUP_GRACE_HOURS`) — короткий рестарт бота
    во время окна отзыва не должен ТЕРЯТЬ приглашение: джоба ставится «почти сейчас»."""
    _ready(tmp_path)
    sid = _run(_make_session(start_offset_min=-90, end_offset_min=-60))  # +10 мин задержки -> уже в прошлом

    async def body(s):
        ok = await sf.schedule_for_session(sid)
        assert ok is True
        job = s.get_job(sf.feedback_job_id(sid))
        assert job is not None
        assert job.trigger.run_date.replace(tzinfo=None) > msk_now()

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_for_session_missing_session_cancels_and_returns_false(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        ok = await sf.schedule_for_session(999999)
        assert ok is False

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Доставка приглашений: только отмеченным, идемпотентно, тумблер, тихие часы
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_deliver_feedback_prompts_noop_when_disabled(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    bot = _with_bot(monkeypatch)

    _run(sf.deliver_feedback_prompts(sid))  # тумблер выключен по умолчанию

    assert bot.sent == []
    assert _run(db.get_session_feedback(DELEGATE_ID, sid)) is None


def test_deliver_feedback_prompts_only_to_marked_delegates(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(_add_delegate(DELEGATE2_ID))
    _run(db.set_setting("session_feedback_enabled", "on"))
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    bot = _with_bot(monkeypatch)

    _run(sf.deliver_feedback_prompts(sid))

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == DELEGATE_ID
    assert _run(db.get_session_feedback(DELEGATE2_ID, sid)) is None


def test_deliver_feedback_prompts_is_idempotent_on_repeat_call(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.set_setting("session_feedback_enabled", "on"))
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    bot = _with_bot(monkeypatch)

    _run(sf.deliver_feedback_prompts(sid))
    _run(sf.deliver_feedback_prompts(sid))  # повторный тик джобы — не дублирует приглашение

    assert len(bot.sent) == 1


def test_deliver_feedback_prompts_respects_quiet_hours(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.set_setting("session_feedback_enabled", "on"))
    now = msk_now()
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", (now - timedelta(minutes=1)).strftime("%H:%M")))
    _run(db.set_setting("quiet_hours_end", (now + timedelta(hours=1)).strftime("%H:%M")))
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    bot = _with_bot(monkeypatch)

    _run(sf.deliver_feedback_prompts(sid))

    assert bot.sent == []  # не ушло сразу — легло в очередь тихих часов
    from database.db import count_pending_delayed_notifications
    assert _run(count_pending_delayed_notifications()) == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Делегатский шов — оценка, чужой callback, комментарий
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sfb_rate_rejects_unmarked_delegate_with_alert(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    cb = FakeCallback(f"sfb:r:{sid}:5", user_id=DELEGATE_ID)
    _run(sf_handlers.sfb_rate(cb))
    assert cb.answers[0][1] is True  # show_alert
    assert "недоступна" in cb.answers[0][0]
    assert _run(db.get_session_feedback(DELEGATE_ID, sid)) is None


def test_sfb_rate_marks_rating_and_offers_comment(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp))

    cb = FakeCallback(f"sfb:r:{sid}:4", user_id=DELEGATE_ID)
    _run(sf_handlers.sfb_rate(cb))

    row = _run(db.get_session_feedback(DELEGATE_ID, sid))
    assert row["rating"] == 4
    assert "пару слов" in cb.message.text


def test_sfb_rate_retap_changes_rating_not_duplicate_row(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp))

    _run(sf_handlers.sfb_rate(FakeCallback(f"sfb:r:{sid}:2", user_id=DELEGATE_ID)))
    _run(sf_handlers.sfb_rate(FakeCallback(f"sfb:r:{sid}:5", user_id=DELEGATE_ID)))

    row = _run(db.get_session_feedback(DELEGATE_ID, sid))
    assert row["rating"] == 5


def test_sfb_offer_comment_rejects_unmarked_delegate(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    state = _fresh_state(DELEGATE_ID)
    cb = FakeCallback(f"sfb:c:{sid}", user_id=DELEGATE_ID)
    _run(sf_handlers.sfb_offer_comment(cb, state))
    assert cb.answers[0][1] is True
    assert _run(state.get_state()) is None


def test_sfb_offer_comment_sets_state_and_hint(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    state = _fresh_state(DELEGATE_ID)
    cb = FakeCallback(f"sfb:c:{sid}", user_id=DELEGATE_ID)
    _run(sf_handlers.sfb_offer_comment(cb, state))

    from handlers.states import SessionFeedbackComment
    assert _run(state.get_state()) == SessionFeedbackComment.waiting.state
    data = _run(state.get_data())
    assert data["sfb_session_id"] == sid
    assert "следующим сообщением" in cb.message.text


def test_sfb_comment_step_saves_and_confirms(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(db.record_session_checkin(DELEGATE_ID, sid, [], source="miniapp"))
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _run(db.create_session_feedback_prompt(DELEGATE_ID, sid, stamp))

    state = _fresh_state(DELEGATE_ID)
    _run(state.set_state(__import__("handlers.states", fromlist=["SessionFeedbackComment"]).SessionFeedbackComment.waiting))
    _run(state.update_data(sfb_session_id=sid))

    msg = FakeMessage(text="Было классно!", user_id=DELEGATE_ID)
    _run(sf_handlers.sfb_comment_step(msg, state))

    row = _run(db.get_session_feedback(DELEGATE_ID, sid))
    assert row["comment"] == "Было классно!"
    assert _run(state.get_state()) is None
    assert any("записал" in a[0] for a in msg.answers)


def test_sfb_comment_step_noop_when_no_session_in_state(tmp_path):
    _ready(tmp_path)
    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="Текст без сессии", user_id=DELEGATE_ID)
    _run(sf_handlers.sfb_comment_step(msg, state))
    assert msg.answers == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# Менеджерский экран — статистика дня + список комментариев
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_day_feedback_screen_lists_sessions_with_stats(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    _run(_mark_prompt_and_set(DELEGATE_ID, sid, rating=5))
    day = msk_now().strftime("%Y-%m-%d")

    text, _kb = _run(sf_handlers.render_day_feedback_screen("msk", day))
    assert "⭐ 5.0" in text
    assert "отмечено 1" in text


def test_prog_fbc_open_shows_comment_and_allows_pagination(tmp_path):
    _ready(tmp_path)
    sid = _run(_make_session())
    for i in range(2):
        tid = DELEGATE_ID + i
        _run(_add_delegate(tid))
        _run(_mark_prompt_and_set(tid, sid, rating=4, comment=f"Отзыв {i}"))

    cb = FakeCallback(f"prog_fbc:{sid}:0", user_id=MANAGER_ID)
    _run(sf_handlers.prog_fbc_open(cb))
    assert "Отзыв 0" in cb.message.text or "Отзыв 1" in cb.message.text


def test_prog_fbday_open_missing_city_shows_empty_state(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("prog_fbday:msk:2026-10-31", user_id=MANAGER_ID)
    _run(sf_handlers.prog_fbday_open(cb))
    assert "Сессий пока нет" in cb.message.text


def test_session_card_hides_comments_button_when_none(tmp_path):
    """Карточка сессии: «💬 Комментарии» — только когда есть хоть один комментарий."""
    from handlers import admin_program

    def _cbs(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    _ready(tmp_path)
    sid = _run(_make_session())
    _run(_add_delegate(DELEGATE_ID))
    _run(_mark_prompt_and_set(DELEGATE_ID, sid, rating=5))  # оценка без комментария
    _text, kb = _run(admin_program.render_session_card(sid))
    assert f"prog_fbc:{sid}:0" not in _cbs(kb)

    _run(_mark_prompt_and_set(DELEGATE_ID + 1, sid, rating=4, comment="Круто"))
    _text, kb = _run(admin_program.render_session_card(sid))
    assert f"prog_fbc:{sid}:0" in _cbs(kb)
