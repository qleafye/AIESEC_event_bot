"""Phase 32 План 8 (D-11/D-26/D-30): три разовые джобы амбассадорской волны в
`services.scheduler` — стартовая рассылка волны, напоминание за сутки до дедлайна задания,
сводка менеджеру в конце волны, + переармирование на старте бота.

Стиль — тот же, что `tests/test_ambassador_wave_rating_32.py` (asyncio.run, временная БД
в tmp_path — pytest-asyncio в этом окружении недоступен) и `tests/test_scheduler_reconcile_
block6.py` (реальный AsyncIOScheduler на временном jobstore для тестов постановки/снятия
джоб; тесты самих ЦЕЛЕЙ джоб — прямой вызов `send_*`, без реального APScheduler).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramForbiddenError

from config import config
from database import db
import services.scheduler as sched
from services import quiet_hours


def _ready(tmp_path, name="wave_scheduling.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, full_name=None, registration_date=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": registration_date or "2026-01-01 00:00:00",
        "event_city": event_city,
    }))


def _make_ambassador(tid, *, since="2025-01-01 00:00:00", **kw):
    _seed_user(tid, **kw)
    _run(db.set_ambassador_flag(tid, active=True, at=since))


class FakeBot:
    """Собирает каждую отправку. `forbidden_ids` — кому send_message бросает
    TelegramForbiddenError (заблокировавший бота), остальные получают сообщение как обычно."""

    def __init__(self, forbidden_ids=frozenset()):
        self.sent = []  # [(chat_id, text, parse_mode, reply_markup)]
        self.forbidden_ids = set(forbidden_ids)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self.forbidden_ids:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self.sent.append((chat_id, text, parse_mode, reply_markup))
        return None


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
    """Собрать реальный AsyncIOScheduler на временном jobstore, запустить его (`.start(paused=
    True)`, тот же приём, что `init_scheduler`) и выполнить `body(s)` — ОДНИМ `asyncio.run`,
    т.к. `AsyncIOScheduler.start()` требует запущенный event loop, а после выхода из
    `asyncio.run` цикл закрывается и синхронные вызовы `add_job` на уже запущенном (не paused)
    планировщике падают на закрытом loop. `body` — асинхронная функция, принимающая `s`."""
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
# Задача 1: старт волны
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_wave_start_job_id_and_replace_existing(tmp_path, monkeypatch):
    run_at = datetime.now() + timedelta(days=1)
    later = run_at + timedelta(hours=1)

    async def body(s):
        sched.schedule_wave_start(7, run_at)
        assert s.get_job("wave_start_7") is not None
        # Повторная постановка той же волны (переармирование, правка дат) заменяет джобу, а
        # не плодит вторую.
        sched.schedule_wave_start(7, later)
        jobs = [j for j in s.get_jobs() if j.id == "wave_start_7"]
        assert len(jobs) == 1
        assert jobs[0].next_run_time.replace(tzinfo=None) == later

    _run_scheduled(tmp_path, monkeypatch, body)


def test_cancel_wave_jobs_removes_start_and_end_fail_soft(tmp_path, monkeypatch):
    run_at = datetime.now() + timedelta(days=1)

    async def body(s):
        sched.schedule_wave_start(7, run_at)
        sched.schedule_wave_end(7, run_at)
        sched.cancel_wave_jobs(7)
        assert s.get_job("wave_start_7") is None
        assert s.get_job("wave_end_7") is None
        sched.cancel_wave_jobs(7)  # ничего не стоит — fail-soft, не падает

    _run_scheduled(tmp_path, monkeypatch, body)


def test_cancel_wave_jobs_also_removes_results_broadcast(tmp_path, monkeypatch):
    """32-FIX-common-2 (хвост IN-07): удаление волны (`handlers/admin_game_waves.py::
    wave_delete_go`) зовёт `cancel_wave_jobs` — если менеджер успел объявить итоги и сразу
    удалить волну, джоба рассылки итогов раньше переживала удаление: `wave_results_broadcast_
    {id}` не входил в список снимаемых id."""
    async def body(s):
        sched.schedule_wave_results_broadcast(7)
        assert s.get_job("wave_results_broadcast_7") is not None
        sched.cancel_wave_jobs(7)
        assert s.get_job("wave_results_broadcast_7") is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_send_wave_start_dm_no_args_are_objects():
    import inspect
    sig = inspect.signature(sched.send_wave_start_dm)
    for p in sig.parameters.values():
        assert p.annotation in (int, inspect._empty) or "int" in str(p.annotation)


def test_send_wave_start_dm_skips_left_ambassador(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_ambassador_flag(1, active=False, at="2026-09-25 00:00:00"))  # вышел до старта

    _run(sched.send_wave_start_dm(wave_id, 1))
    assert bot.sent == []


def test_send_wave_start_dm_skips_joined_after_wave_started(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _make_ambassador(2, since="2026-10-05 00:00:00")  # стал амбассадором ПОСЛЕ старта волны

    _run(sched.send_wave_start_dm(wave_id, 2))
    assert bot.sent == []


def test_send_wave_start_dm_skips_draft_wave(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    # состояние не переведено из 'draft'
    _run(sched.send_wave_start_dm(wave_id, 1))
    assert bot.sent == []


def test_send_wave_start_dm_one_message_one_button_no_intro(tmp_path, monkeypatch):
    """D-11: волна без вводного текста — сообщение без пустой строки/обрубка."""
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.create_task("Task", "Light", 10, "photo", "2026-10-05 12:00:00", None, wave_id=wave_id))

    _run(sched.send_wave_start_dm(wave_id, 1))
    assert len(bot.sent) == 1
    chat_id, text, parse_mode, markup = bot.sent[0]
    assert chat_id == 1
    assert parse_mode == "HTML"
    assert markup is not None and len(markup.inline_keyboard) == 1
    assert len(markup.inline_keyboard[0]) == 1
    assert "\n\n\n" not in text
    assert "Task" in text


def test_send_wave_start_dm_intro_text_present(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-08 00:00:00", intro_text="Добро пожаловать в волну!",
    ))
    _run(db.set_wave_state(wave_id, "active"))
    _run(sched.send_wave_start_dm(wave_id, 1))
    text = bot.sent[0][1]
    assert "Добро пожаловать в волну!" in text


def test_send_wave_start_dm_task_without_deadline_labeled(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.create_task(
        "NoDeadline", "Light", 5, "photo", db.NO_DEADLINE_AT, None, wave_id=wave_id,
    ))
    _run(sched.send_wave_start_dm(wave_id, 1))
    text = bot.sent[0][1]
    assert "без срока" in text
    assert "до без срока" not in text  # WR-12: приклеенное "до " не должно дублировать текст


def test_send_wave_start_dm_english_ambassador_gets_translated_task_line(tmp_path, monkeypatch):
    """32-FIX-common-2 (хвост IN-06): «баллов»/«до» собирались f-строкой ПОВЕРХ уже переведённого
    шаблона (`i18n.fill_template` подставляет {tasks} ПОСЛЕ перевода шаблона) — англоязычный
    амбассадор видел строку задания русской, даже когда сам шаблон переведён. Проверяем именно
    строку задания (единственное, что чинит этот фикс) — перевод самого шаблона `wave_start_
    message_text` зависит от того, засеяна ли `database.db.translations`, что вне этого теста."""
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    _run(db.set_setting("delegate_lang_enabled", "on"))
    _run(db.set_user_lang(1, "en"))
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.create_task("Task", "Light", 10, "photo", "2026-10-05 12:00:00", None, wave_id=wave_id))

    _run(sched.send_wave_start_dm(wave_id, 1))
    text = bot.sent[0][1]
    assert "Task — 10 points, until 05.10" in text
    assert "10 баллов" not in text
    assert "до 05.10" not in text


def test_send_wave_start_dm_intro_html_not_double_escaped(tmp_path, monkeypatch):
    """WR-09: `intro_text` в БД — уже готовый HTML (`message.html_text` на записи); повторный
    `html.escape` на показе превращал форматирование менеджера в буквальные `&amp;`/`<b>`."""
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-08 00:00:00",
        intro_text="Зови друзей & делай контент <b>активно</b>",
    ))
    _run(db.set_wave_state(wave_id, "active"))
    _run(sched.send_wave_start_dm(wave_id, 1))
    text = bot.sent[0][1]
    assert "Зови друзей & делай контент <b>активно</b>" in text
    assert "&amp;" not in text


def test_send_wave_start_blocked_recipient_does_not_abort_others(tmp_path, monkeypatch):
    """Регрессия: заблокировавший бота получатель — свой try/except в `send_wave_start_dm`,
    фан-аут в `send_wave_start` не обрывается на нём."""
    _ready(tmp_path)
    _make_ambassador(1)
    _make_ambassador(2)
    bot = _with_bot(monkeypatch, FakeBot(forbidden_ids={1}))
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 1, 0, 0, 1))

    _run(sched.send_wave_start(wave_id))
    assert [c[0] for c in bot.sent] == [2]


def test_send_wave_start_includes_ambassador_who_joined_after_scheduling(tmp_path, monkeypatch):
    """Регрессия ГЛАВНОГО бага фикса: джоба волны поставлена, когда участников ещё нет; амбассадор
    появляется ПОСЛЕ постановки, но ДО `starts_at` волны — он полноправный участник по
    `wave_eligible`, и `send_wave_start` (одна джоба на волну, получатели разворачиваются на
    срабатывании) его находит и отправляет ему сообщение."""
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))

    async def body(s):
        await sched.schedule_wave_start_for_all(wave_id)  # постановка — участников пока нет
        await db.add_user({
            "telegram_id": 1,
            "full_name": "Delegate 1",
            "registration_date": "2026-01-01 00:00:00",
            "event_city": None,
        })
        await db.set_ambassador_flag(1, active=True, at="2026-09-28 00:00:00")  # ДО starts_at
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 1, 0, 0, 1))
        await sched.send_wave_start(wave_id)

    _run_scheduled(tmp_path, monkeypatch, body)
    assert [c[0] for c in bot.sent] == [1]


def test_send_wave_start_marks_once_second_call_sends_nothing(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 1, 0, 0, 1))

    _run(sched.send_wave_start(wave_id))
    wave = _run(db.get_wave(wave_id))
    assert wave["started_notified_at"]
    assert len(bot.sent) == 1

    _run(sched.send_wave_start(wave_id))  # повторное срабатывание/переармирование — тишина
    assert len(bot.sent) == 1


def test_send_wave_start_future_starts_at_reschedules_no_send(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-10 00:00:00", "2026-10-20 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 1, 0, 0, 0))

    async def body(s):
        sched.schedule_wave_start(wave_id, datetime(2026, 10, 1, 0, 1, 0))  # ошибочно рано
        await sched.send_wave_start(wave_id)
        job = s.get_job(f"wave_start_{wave_id}")
        assert job is not None
        assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 10, 0, 0, 0)

    _run_scheduled(tmp_path, monkeypatch, body)
    assert bot.sent == []
    wave = _run(db.get_wave(wave_id))
    assert wave["started_notified_at"] is None


def test_send_wave_start_draft_or_missing_wave_silent(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    # состояние остаётся 'draft'
    _run(sched.send_wave_start(wave_id))
    assert bot.sent == []

    _run(sched.send_wave_start(999999))  # такой волны нет вовсе
    assert bot.sent == []


def test_send_wave_start_dm_quiet_hours_queues_not_cancels(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_setting("quiet_hours_enabled", "on"))  # default-окно 22:00-09:00

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 2, 23, 0, 0))
    _run(sched.send_wave_start_dm(wave_id, 1))
    assert bot.sent == []  # не отправлено сейчас — положено в очередь тихих часов

    async def _count():
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM delayed_notifications") as cur:
                row = await cur.fetchone()
                return row[0]
    assert _run(_count()) == 1


def test_schedule_wave_start_for_all_no_mark_replaces_single_job(tmp_path, monkeypatch):
    """Постановка джобы — не отправка: `started_notified_at` не встаёт, повторная постановка
    (переармирование) заменяет ту же джобу `wave_start_{id}`, а не плодит вторую."""
    _ready(tmp_path)
    _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))

    async def body(s):
        n1 = await sched.schedule_wave_start_for_all(wave_id)
        wave = await db.get_wave(wave_id)
        assert wave["started_notified_at"] is None
        n2 = await sched.schedule_wave_start_for_all(wave_id)  # повторная постановка
        jobs = [j for j in s.get_jobs() if j.id == f"wave_start_{wave_id}"]
        return n1, n2, len(jobs)

    n1, n2, job_count = _run_scheduled(tmp_path, monkeypatch, body)
    assert n1 == 1 and n2 == 1
    assert job_count == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: напоминание за сутки до дедлайна
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_task_deadline_reminder_no_job_for_task_without_deadline(tmp_path, monkeypatch):
    _ready(tmp_path, "t2a.db")
    task_id = _run(db.create_task("T", "Light", 5, "photo", db.NO_DEADLINE_AT, None))
    # `game_labels.task_deadline()` на NO_DEADLINE_AT даёт None — вызывающий код
    # (send_task_deadline_reminder/reconcile_wave_jobs) тогда вовсе не зовёт schedule_*;
    # здесь напрямую проверяем второй контракт schedule_task_deadline_reminder: момент уже в
    # прошлом -> джоба не ставится (тот же ранний выход, что дал бы NO_DEADLINE_AT в реальном
    # вызывающем коде).
    past = datetime.now() - timedelta(hours=1)

    async def body(s):
        assert sched.schedule_task_deadline_reminder(task_id, past) is False
        assert s.get_job(f"task_deadline_reminder_{task_id}") is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_task_deadline_reminder_replace_not_duplicate(tmp_path, monkeypatch):
    deadline = datetime.now() + timedelta(days=2)

    async def body(s):
        ok1 = sched.schedule_task_deadline_reminder(55, deadline)
        ok2 = sched.schedule_task_deadline_reminder(55, deadline + timedelta(hours=3))
        assert ok1 is True and ok2 is True
        jobs = [j for j in s.get_jobs() if j.id == "task_deadline_reminder_55"]
        assert len(jobs) == 1

    _run_scheduled(tmp_path, monkeypatch, body)


def test_send_task_deadline_reminder_submitted_user_skipped_not_submitted_gets_it(tmp_path, monkeypatch):
    _ready(tmp_path, "t2c.db")
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    _make_ambassador(2)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    deadline_at = "2026-10-05 12:00:00"
    task_id = _run(db.create_task("Deadline task", "Light", 10, "photo", deadline_at, None, wave_id=wave_id))
    _run(db.create_submission(task_id, 1, "text", "готово", "2026-10-02 00:00:00"))  # user 1 сдал

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 4, 12, 0, 0))
    _run(sched.send_task_deadline_reminder(task_id))
    recipients = [c[0] for c in bot.sent]
    assert 1 not in recipients
    assert 2 in recipients


def test_send_task_deadline_reminder_rejected_submission_counts_as_not_submitted(tmp_path, monkeypatch):
    _ready(tmp_path, "t2d.db")
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    deadline_at = "2026-10-05 12:00:00"
    task_id = _run(db.create_task("Deadline task", "Light", 10, "photo", deadline_at, None, wave_id=wave_id))
    sub_id = _run(db.create_submission(task_id, 1, "text", "черновик", "2026-10-02 00:00:00"))
    _run(db.claim_submission(sub_id, 999, "rejected", reject_reason="мимо темы"))

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 4, 12, 0, 0))
    _run(sched.send_task_deadline_reminder(task_id))
    assert [c[0] for c in bot.sent] == [1]  # отклонённая сдача — не сдал, напоминание уходит


def test_send_task_deadline_reminder_deadline_already_passed_sends_nothing(tmp_path, monkeypatch):
    """WR-13(в): дедлайн сдвинули РАНЬШЕ уже после постановки джобы — «скоро дедлайн» ПОСЛЕ
    самого дедлайна вводит в заблуждение, джоба не шлёт и не пытается переставить себя на
    прошедший момент."""
    _ready(tmp_path, "t2h.db")
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    task_id = _run(db.create_task(
        "AlreadyDue", "Light", 10, "photo", "2026-10-04 12:00:00", None, wave_id=wave_id,
    ))

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 5, 0, 0, 0))
    _run(sched.send_task_deadline_reminder(task_id))
    assert bot.sent == []


def test_send_task_deadline_reminder_manager_template_with_stray_brace_does_not_abort(tmp_path, monkeypatch):
    """CR-05: менеджерский текст с посторонней `{` не должен ронять напоминание целиком —
    `.replace`-подстановка (`game_labels.fill_template`) оставляет неизвестный плейсхолдер как
    есть, а не поднимает `KeyError`/`ValueError`, как `.format()`."""
    _ready(tmp_path, "t2i.db")
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    _run(db.set_setting("wave_deadline_reminder_text", "пиши в чат {ссылка} до {deadline}"))
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    task_id = _run(db.create_task(
        "Task", "Light", 10, "photo", "2026-10-05 12:00:00", None, wave_id=wave_id,
    ))

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 4, 12, 0, 0))
    _run(sched.send_task_deadline_reminder(task_id))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "{ссылка}" in text  # неизвестный плейсхолдер остался как есть, не упал
    assert "{deadline}" not in text  # известный — подставлен


def test_send_task_deadline_reminder_deadline_pushed_forward_reschedules(tmp_path, monkeypatch):
    _ready(tmp_path, "t2e.db")
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-20 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    task_id = _run(db.create_task(
        "Pushed", "Light", 10, "photo", "2026-10-06 12:00:00", None, wave_id=wave_id,
    ))

    # Прямая правка дедлайна через UPDATE — не зависим от точного имени аксессора правки
    # задания (вне files_modified этого плана).
    async def _bump():
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE game_tasks SET deadline_at = ? WHERE id = ?",
                ("2026-10-15 12:00:00", task_id),
            )
            await conn.commit()
    _run(_bump())

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 5, 12, 0, 0))

    async def body(s):
        await sched.send_task_deadline_reminder(task_id)
        assert bot.sent == []  # рано: до нового дедлайна больше суток — переставили, не шлём
        job = s.get_job(f"task_deadline_reminder_{task_id}")
        assert job is not None
        assert job.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 14, 12, 0, 0)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_send_task_deadline_reminder_out_of_wave_default_city_legacy_delegate_gets_it(
    tmp_path, monkeypatch,
):
    """32-FIX-common-2 (хвост WR-03): задание ВНЕ волн привязано к дефолтному городу
    (`event_city="msk"`), делегат — легаси-строка с `event_city IS NULL`. Он видит это же
    задание в списке (`list_active_tasks(city_scope=...)` уже нормализует пустой город в
    дефолтный), но раньше `_task_out_of_wave_recipients` сравнивал `u["event_city"] == city`
    сырым равенством — `None != "msk"` молча выкидывал его из напоминания. Делегат другого
    (не дефолтного) города остаётся исключён."""
    _ready(tmp_path, "t2j.db")
    bot = _with_bot(monkeypatch)
    _run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1, event_city=None)  # легаси дефолтного города (msk) — как ~590 строк на проде
    _seed_user(2, event_city="spb")  # другой город — не должен получить
    task_id = _run(db.create_task(
        "Задание вне волн", "Light", 10, "photo", "2026-10-05 12:00:00", None, event_city="msk",
    ))

    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 4, 12, 0, 0))
    _run(sched.send_task_deadline_reminder(task_id))
    recipients = [c[0] for c in bot.sent]
    assert recipients == [1]


def test_reconcile_wave_jobs_idempotent_expected_job_ids_no_sends(tmp_path, monkeypatch):
    _ready(tmp_path, "t2f.db")
    bot = _with_bot(monkeypatch)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    task_id = _run(db.create_task(
        "Reconciled", "Light", 10, "photo", "2026-10-20 12:00:00", None, wave_id=wave_id,
    ))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 20, 12, 0, 0))

    async def body(s):
        await sched.reconcile_wave_jobs()
        ids_first = {j.id for j in s.get_jobs()}
        assert f"wave_end_{wave_id}" in ids_first
        assert f"task_deadline_reminder_{task_id}" in ids_first
        assert f"wave_start_{wave_id}" in ids_first
        assert bot.sent == []  # постановка джоб — не отправка

        await sched.reconcile_wave_jobs()  # второй вызов — идемпотентно, тот же набор id
        ids_second = {j.id for j in s.get_jobs()}
        assert ids_first == ids_second
        assert bot.sent == []

    _run_scheduled(tmp_path, monkeypatch, body)


def test_reconcile_wave_jobs_rearms_out_of_wave_task_with_future_deadline(tmp_path, monkeypatch):
    """32-FIX-common-2 (хвост WR-13б): раньше переармирование при старте бота обходило только
    задания активных волн (`list_wave_tasks` по каждой `active`-волне) — задание ВНЕ волн
    (`wave_id` пуст) со сроком в будущем после пересозданного `jobs.sqlite`/долгого простоя
    оставалось без джобы напоминания навсегда, до ручной правки задания."""
    _ready(tmp_path, "t2g.db")
    _with_bot(monkeypatch)
    out_of_wave_id = _run(db.create_task(
        "Вне волн", "Light", 10, "photo", "2026-10-20 12:00:00", None,
    ))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 20, 12, 0, 0))

    async def body(s):
        await sched.reconcile_wave_jobs()
        assert s.get_job(f"task_deadline_reminder_{out_of_wave_id}") is not None

        await sched.reconcile_wave_jobs()  # второй вызов — идемпотентно, джоба не задваивается
        jobs = [j for j in s.get_jobs() if j.id == f"task_deadline_reminder_{out_of_wave_id}"]
        assert len(jobs) == 1

    _run_scheduled(tmp_path, monkeypatch, body)


def test_reconcile_wave_jobs_does_not_resurrect_past_out_of_wave_deadline(tmp_path, monkeypatch):
    """Прошедшее напоминание вне волн не воскрешается — тот же контракт, что и у волновых
    заданий (`schedule_task_deadline_reminder` сама отказывается ставить джобу в прошлое)."""
    _ready(tmp_path, "t2h2.db")
    _with_bot(monkeypatch)
    past_deadline_id = _run(db.create_task(
        "Просрочено вне волн", "Light", 10, "photo", "2026-09-01 12:00:00", None,
    ))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 20, 12, 0, 0))

    async def body(s):
        await sched.reconcile_wave_jobs()
        assert s.get_job(f"task_deadline_reminder_{past_deadline_id}") is None

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: сводка менеджеру в конце волны
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_wave_end_ping_close_wave_false_no_send(tmp_path, monkeypatch):
    _ready(tmp_path, "t3a.db")
    bot = _with_bot(monkeypatch)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    # состояние остаётся 'draft' — close_wave требует expected_state='active'

    async def fake_holders(cap, *, city=None):
        return [42]
    monkeypatch.setattr("handlers.admin_caps.capability_holders", fake_holders)

    _run(sched.send_wave_end_ping(wave_id))
    assert bot.sent == []


def test_send_wave_end_ping_only_city_managers_and_has_numbers(tmp_path, monkeypatch):
    """Известный пред-существующий баг ВНЕ этого плана (`services/ambassador_waves.py::
    wave_rating` зовёт `list_ambassadors(city_scope=wave.get("event_city"))` сырой строкой, а
    не дескриптором `cities.city_scope(...)` — падает `database.db._city_clause` на любой
    волне с реальным городом; см. SUMMARY, раздел «Найденный, не исправленный баг») — файл
    вне `files_modified` этого плана, править нельзя. `wave_end_summary` здесь замокан, чтобы
    тест проверял ИМЕННО код `send_wave_end_ping` (city-роутинг в `capability_holders`,
    состав текста), не наступая на эту чужую дыру."""
    _ready(tmp_path, "t3b.db")
    bot = _with_bot(monkeypatch)
    wave_id = _run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-08 00:00:00", event_city="msk",
    ))
    _run(db.set_wave_state(wave_id, "active"))

    async def fake_summary(wid):
        wave = await db.get_wave(wid)
        return {"wave": wave, "top": [{"place": 1, "name": "Иван", "user_id": 1, "points": 30}],
                "pending": 2, "pending_near_cutoff": 0}
    monkeypatch.setattr("services.ambassador_waves.wave_end_summary", fake_summary)

    seen_city = {}

    async def fake_holders(cap, *, city=None):
        seen_city["city"] = city
        return [42]
    monkeypatch.setattr("handlers.admin_caps.capability_holders", fake_holders)

    _run(sched.send_wave_end_ping(wave_id))
    assert seen_city["city"] == "msk"
    assert len(bot.sent) == 1
    chat_id, text, parse_mode, markup = bot.sent[0]
    assert chat_id == 42
    assert "Волна" in text
    assert "Волна Волна" not in text  # WR-10: {wave} — только номер, слово уже в шаблоне
    assert markup is not None and len(markup.inline_keyboard) == 2
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert f"wavefin:{wave_id}" in callbacks
    assert "admin_game_review" in callbacks


def test_send_wave_end_ping_double_fire_exactly_one_send(tmp_path, monkeypatch):
    _ready(tmp_path, "t3c.db")
    bot = _with_bot(monkeypatch)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))

    async def fake_holders(cap, *, city=None):
        return [42]
    monkeypatch.setattr("handlers.admin_caps.capability_holders", fake_holders)

    _run(sched.send_wave_end_ping(wave_id))  # первое срабатывание — active -> closing
    _run(sched.send_wave_end_ping(wave_id))  # второе (повторный тик/переармирование) — no-op
    assert len(bot.sent) == 1


def test_send_wave_end_ping_quiet_hours_queues_but_still_closes_wave(tmp_path, monkeypatch):
    """32-FIX-common-2 (хвост IN-09а): переход `active -> closing` — сразу, тихие часы
    откладывают только саму отправку менеджерам (тот же приём, что `send_wave_start_dm`).
    Раньше сообщение уходило напрямую через `_safe_send`, мимо тихих часов, ровно в момент
    конца волны (23:59:59 по умолчанию — самое обычное время для тихих часов менеджера)."""
    _ready(tmp_path, "t3e.db")
    bot = _with_bot(monkeypatch)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_setting("quiet_hours_enabled", "on"))  # default-окно 22:00-09:00

    async def fake_holders(cap, *, city=None):
        return [42]
    monkeypatch.setattr("handlers.admin_caps.capability_holders", fake_holders)
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 8, 23, 0, 0))

    _run(sched.send_wave_end_ping(wave_id))

    assert bot.sent == []  # не отправлено сейчас — положено в очередь тихих часов
    wave = _run(db.get_wave(wave_id))
    assert wave["state"] == "closing"  # переход состояния не ждёт утра

    async def _count():
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM delayed_notifications") as cur:
                row = await cur.fetchone()
                return row[0]
    assert _run(_count()) == 1


def test_send_wave_end_ping_escapes_name(tmp_path, monkeypatch):
    _ready(tmp_path, "t3d.db")
    bot = _with_bot(monkeypatch)
    _seed_user(1, full_name="<b>Имя</b>")
    _run(db.set_ambassador_flag(1, active=True, at="2025-01-01 00:00:00"))
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    task_id = _run(db.create_task(
        "T", "Light", 10, "photo", "2026-10-05 12:00:00", None, wave_id=wave_id,
    ))
    sub_id = _run(db.create_submission(task_id, 1, "text", "готово", "2026-10-02 00:00:00"))
    _run(db.claim_submission(sub_id, 999, "approved", coins_awarded=10))
    _run(db.add_coins(1, 10, source="task", task_id=task_id))

    async def fake_holders(cap, *, city=None):
        return [42]
    monkeypatch.setattr("handlers.admin_caps.capability_holders", fake_holders)

    _run(sched.send_wave_end_ping(wave_id))
    text = bot.sent[0][1]
    assert "<b>Имя</b>" not in text
    assert "&lt;b&gt;" in text


def test_schedule_wave_end_replace_existing(tmp_path, monkeypatch):
    d1 = datetime.now() + timedelta(days=5)
    d2 = d1 + timedelta(days=1)

    async def body(s):
        sched.schedule_wave_end(9, d1)
        sched.schedule_wave_end(9, d2)  # правка даты волны — перезаписывает, не дублирует
        jobs = [j for j in s.get_jobs() if j.id == "wave_end_9"]
        assert len(jobs) == 1
        assert jobs[0].next_run_time.replace(tzinfo=None) == d2

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_wave_end_past_date_catches_up_now_plus_minute(tmp_path, monkeypatch):
    """WR-05: `ends_at` в прошлом (бот лежал дольше `_MISFIRE_GRACE_SECONDS`, `jobs.sqlite`
    пересоздан, волну завели/запустили задним числом) — та же ловушка и тот же приём, что уже
    применяется к старту волны: «сейчас + минута», а не просроченный `run_date`, который
    APScheduler тихо выбрасывает как misfire (волна оставалась `active` навсегда)."""
    now = datetime(2026, 10, 10, 12, 0, 0)
    past = now - timedelta(days=3)
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: now)

    async def body(s):
        sched.schedule_wave_end(9, past)
        job = s.get_job("wave_end_9")
        assert job is not None
        assert job.next_run_time.replace(tzinfo=None) == now + timedelta(minutes=1)

    _run_scheduled(tmp_path, monkeypatch, body)
