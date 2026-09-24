"""Форум-ночь п.3 (D-03, идея №2, `.planning/FORUM-CHECKIN.md`,
`.planning/IDEAS-CHECKIN-BACKLOG-260924.md` А2 идея №2): рассылка QR перед форумом (вечерняя,
на город) + утренний повтор неподтвердившим + подтверждение «✅ Сохранил, открывается».

Стиль — `tests/test_ambassador_wave_scheduling_32.py` (реальный AsyncIOScheduler на временном
jobstore для планирования/снятия джоб) + `tests/test_checkin_db_260924.py` (шаблонная БД,
`asyncio.run`, pytest-asyncio недоступен в этом окружении)."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

from config import config
from database import db
import services.scheduler as sched
import services.checkin_broadcast as cb
from tests._dbtpl import fast_init_db

UID = 260924101


def _ready(tmp_path, name="checkin_qr_broadcast.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, full_name=None, status="approved", season=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": "2026-01-01 00:00:00",
        "event_city": event_city,
    }))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE users SET status = ?, season = ? WHERE telegram_id = ?",
        (status, season, tid),
    )
    conn.commit()
    conn.close()


async def _set_setting(key, value):
    await db.set_setting(key, value)


class FakeBot:
    """Собирает каждую отправку фото — тот же приём, что `FakeBot` в
    `test_ambassador_wave_scheduling_32.py`, расширенный на `send_photo` (QR — картинка)."""

    def __init__(self):
        self.photos = []  # [(chat_id, caption, reply_markup)]

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.photos.append((chat_id, caption, reply_markup))
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
# Task 1: чистые хелперы (даты/id джоб)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_job_ids_default_to_all_without_city():
    assert cb.evening_job_id("spb") == "checkin_qr_evening:spb"
    assert cb.evening_job_id(None) == "checkin_qr_evening:all"
    assert cb.morning_job_id(None) == "checkin_qr_morning:all"


def test_evening_run_at_is_day_before_at_given_time():
    dt = cb.evening_run_at("03.10.2026", "18:00")
    assert dt == datetime(2026, 10, 2, 18, 0, 0)


def test_morning_run_at_is_same_day_at_given_time():
    dt = cb.morning_run_at("03.10.2026", "08:00")
    assert dt == datetime(2026, 10, 3, 8, 0, 0)


def test_run_at_none_without_forum_date():
    assert cb.evening_run_at(None, "18:00") is None
    assert cb.morning_run_at(None, "08:00") is None


def test_run_at_none_on_unparseable_date():
    assert cb.evening_run_at("не дата", "18:00") is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: планирование джоб — дата форума задана/не задана, тумблер выключен
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_city_jobs_skipped_without_forum_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("checkin_qr_enabled", "on"))

    async def body(s):
        result = await cb.schedule_city_jobs(None)
        assert result == {"scheduled": False, "reason": "no_date"}
        assert s.get_job(cb.evening_job_id(None)) is None
        assert s.get_job(cb.morning_job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_city_jobs_creates_both_jobs_with_forum_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("checkin_qr_enabled", "on"))
    _run(_set_setting("forum_date", "03.10.2026"))
    _run(_set_setting("checkin_qr_broadcast_time", "18:00"))
    _run(_set_setting("checkin_qr_morning_repeat_time", "08:00"))

    async def body(s):
        result = await cb.schedule_city_jobs(None)
        assert result["scheduled"] is True
        ev_job = s.get_job(cb.evening_job_id(None))
        morn_job = s.get_job(cb.morning_job_id(None))
        assert ev_job is not None and morn_job is not None
        assert ev_job.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 2, 18, 0, 0)
        assert morn_job.next_run_time.replace(tzinfo=None) == datetime(2026, 10, 3, 8, 0, 0)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_city_jobs_removed_when_master_toggle_off(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("checkin_qr_enabled", "off"))
    _run(_set_setting("forum_date", "03.10.2026"))

    async def body(s):
        result = await cb.schedule_city_jobs(None)
        assert result == {"scheduled": False, "reason": "disabled"}
        assert s.get_job(cb.evening_job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_city_jobs_reschedules_on_forum_date_change(tmp_path, monkeypatch):
    """«Джоба переставляется при смене даты форума» — повторный вызов с НОВОЙ датой заменяет
    прежнюю (тот же replace_existing=True приём, что у wave_start)."""
    _ready(tmp_path)
    _run(_set_setting("checkin_qr_enabled", "on"))
    _run(_set_setting("forum_date", "03.10.2026"))

    async def body(s):
        await cb.schedule_city_jobs(None)
        await _set_setting("forum_date", "10.10.2026")
        await cb.schedule_city_jobs(None)
        jobs = [j for j in s.get_jobs() if j.id == cb.evening_job_id(None)]
        assert len(jobs) == 1
        assert jobs[0].next_run_time.replace(tzinfo=None) == datetime(2026, 10, 9, 18, 0, 0)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_city_jobs_removed_when_forum_date_cleared(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set_setting("checkin_qr_enabled", "on"))
    _run(_set_setting("forum_date", "03.10.2026"))

    async def body(s):
        await cb.schedule_city_jobs(None)
        assert s.get_job(cb.evening_job_id(None)) is not None
        await db.delete_setting("forum_date")
        result = await cb.schedule_city_jobs(None)
        assert result == {"scheduled": False, "reason": "no_date"}
        assert s.get_job(cb.evening_job_id(None)) is None

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 3: аудитория — только одобренные текущего сезона (через checkin_denial)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_eligible_recipients_filters_status_and_season(tmp_path):
    _ready(tmp_path)
    _run(_set_setting("event_season", "YL'26"))
    _seed_user(UID, status="approved", season="YL'26")
    _seed_user(UID + 1, status="approved", season=None)  # без сезона — текущий (fail-soft)
    _seed_user(UID + 2, status="approved", season="YL'25")  # прошлый сезон
    _seed_user(UID + 3, status="pending", season="YL'26")  # не одобрен

    eligible = _run(cb.eligible_recipients(None))
    ids = {u["telegram_id"] for u in eligible}
    assert ids == {UID, UID + 1}


def test_eligible_recipients_scoped_by_city(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, event_city="spb", status="approved")
    _seed_user(UID + 1, event_city="tyumen", status="approved")

    eligible_spb = _run(cb.eligible_recipients("spb"))
    assert {u["telegram_id"] for u in eligible_spb} == {UID}


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 4: отправка — идемпотентность вечерней рассылки/ручной кнопки
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_broadcast_marks_sent_and_is_idempotent(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _seed_user(UID + 1, status="approved")
    bot = _with_bot(monkeypatch)

    result1 = _run(cb.send_broadcast(None))
    assert result1["sent"] == 2
    assert len(bot.photos) == 2
    sent_ids = _run(db.checkin_qr_sent_ids())
    assert sent_ids == {UID, UID + 1}

    # Повторный запуск (рестарт/повторный тап «Разослать сейчас») не шлёт дважды.
    result2 = _run(cb.send_broadcast(None))
    assert result2["sent"] == 0
    assert len(bot.photos) == 2  # не выросло


def test_send_broadcast_confirm_button_attached():
    from aiogram.types import InlineKeyboardMarkup
    kb = cb._confirm_kb()
    assert isinstance(kb, InlineKeyboardMarkup)
    assert kb.inline_keyboard[0][0].callback_data == cb.CONFIRM_CALLBACK


def test_send_broadcast_only_targets_eligible(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _seed_user(UID + 1, status="pending")
    bot = _with_bot(monkeypatch)

    result = _run(cb.send_broadcast(None))
    assert result["sent"] == 1
    assert bot.photos[0][0] == UID


def test_pending_broadcast_count_excludes_already_sent(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _seed_user(UID + 1, status="approved")
    _with_bot(monkeypatch)

    assert _run(cb.pending_broadcast_count(None)) == 2
    _run(cb.send_broadcast(None))
    assert _run(cb.pending_broadcast_count(None)) == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 5: подтверждение + утренний повтор только неподтвердившим
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_confirm_receipt_idempotent(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _with_bot(monkeypatch)
    _run(cb.send_broadcast(None))

    assert _run(cb.confirm_receipt(UID)) is True   # первое подтверждение
    assert _run(cb.confirm_receipt(UID)) is False  # повторный тап — уже было


def test_confirm_receipt_without_prior_send_is_noop():
    assert _run(cb.confirm_receipt(999999999)) is False


def test_send_morning_repeat_only_unconfirmed(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _seed_user(UID + 1, status="approved")
    bot = _with_bot(monkeypatch)
    _run(cb.send_broadcast(None))
    assert len(bot.photos) == 2

    _run(cb.confirm_receipt(UID))  # UID подтвердил, UID+1 — нет

    result = _run(cb.send_morning_repeat(None))
    assert result["sent"] == 1
    assert bot.photos[-1][0] == UID + 1  # только неподтвердивший получил повтор


def test_send_morning_repeat_empty_when_all_confirmed(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _with_bot(monkeypatch)
    _run(cb.send_broadcast(None))
    _run(cb.confirm_receipt(UID))

    result = _run(cb.send_morning_repeat(None))
    assert result["sent"] == 0
    assert result["total"] == 0


def test_send_morning_repeat_includes_newly_approved_and_never_sent(tmp_path, monkeypatch):
    """Находка ревью 260924: делегат, одобренный ПОСЛЕ вечерней рассылки (или у кого вечерняя
    отправка сорвалась — тот же случай, «строки checkin_qr_sends никогда не было»), не должен
    навсегда пропускать утренний повтор — старая версия смотрела только на
    `checkin_qr_unconfirmed_ids` (строка есть, не подтверждена), эта версия обязана взять его
    из полного `eligible_recipients`."""
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _seed_user(UID + 1, status="pending")  # ещё не одобрен на момент вечерней рассылки
    bot = _with_bot(monkeypatch)

    _run(cb.send_broadcast(None))
    assert len(bot.photos) == 1  # только UID получил QR вечером
    _run(cb.confirm_receipt(UID))  # UID подтвердил — не должен получить повтор

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = 'approved' WHERE telegram_id = ?", (UID + 1,))
    conn.commit()
    conn.close()

    result = _run(cb.send_morning_repeat(None))
    assert result["sent"] == 1
    assert bot.photos[-1][0] == UID + 1

    sent_ids = _run(db.checkin_qr_sent_ids())
    assert sent_ids == {UID, UID + 1}  # утренний повтор сам завёл строку новичку


def test_send_morning_repeat_skips_delegate_who_lost_admission(tmp_path, monkeypatch):
    """Делегата отозвали (approved -> rejected) между вечером и утром — повтор его пропускает,
    но не трогает его уже существующую строку checkin_qr_sends."""
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _with_bot(monkeypatch)
    _run(cb.send_broadcast(None))

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = 'rejected' WHERE telegram_id = ?", (UID,))
    conn.commit()
    conn.close()

    result = _run(cb.send_morning_repeat(None))
    assert result["sent"] == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 6: счётчики «QR получили N · подтвердили M»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_checkin_qr_send_counts(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    _seed_user(UID + 1, status="approved")
    _with_bot(monkeypatch)
    _run(cb.send_broadcast(None))
    _run(cb.confirm_receipt(UID))

    got, confirmed = _run(db.checkin_qr_send_counts())
    assert (got, confirmed) == (2, 1)


def test_confirm_button_literal_matches_callback():
    """`handlers/user_actions.py::checkin_qr_confirm_receipt` использует ЛИТЕРАЛ
    "checkinqr_confirm" в декораторе (golden-снимок роутеров разбирает исходный текст, не
    значение переменной) — этот литерал обязан побайтово совпадать с
    `services.checkin_broadcast.CONFIRM_CALLBACK`, иначе кнопка на рассылке никто не поймает."""
    import ast
    import inspect
    import handlers.user_actions as ua

    tree = ast.parse(inspect.getsource(ua))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "checkin_qr_confirm_receipt":
            for deco in node.decorator_list:
                deco_src = ast.unparse(deco)
                assert cb.CONFIRM_CALLBACK in deco_src, deco_src
                found = True
    assert found


def test_send_broadcast_translates_caption_for_english_delegate(tmp_path, monkeypatch):
    """Задача 3 (D-03): текст рассылки уходит через тот же перевод, что «🎟 Мой QR»
    (`handlers.reg_i18n.tr_text`) — делегат с языком «en» получает английскую подпись
    (`services.i18n_form_manual.FORM_DEFAULT_EN`), а не русский дефолт как есть."""
    from services.i18n_form_manual import FORM_DEFAULT_EN, seed

    _ready(tmp_path)
    _run(_set_setting("delegate_lang_enabled", "on"))
    _run(seed())
    _seed_user(UID, status="approved")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET lang = 'en' WHERE telegram_id = ?", (UID,))
    conn.commit()
    conn.close()
    bot = _with_bot(monkeypatch)

    _run(cb.send_broadcast(None))

    assert len(bot.photos) == 1
    default_ru = (
        "Завтра форум! Вот твой QR для входа. Открой его сейчас и сделай скриншот — "
        "на площадке может не быть сети."
    )
    _tid, caption, _kb = bot.photos[0]
    assert caption == FORM_DEFAULT_EN[default_ru]
    assert caption != default_ru


def test_checkin_qr_mark_sent_is_idempotent(tmp_path):
    _ready(tmp_path)
    _seed_user(UID, status="approved")
    first = _run(db.checkin_qr_mark_sent(UID, "spb", "2026-10-02 18:00:00"))
    second = _run(db.checkin_qr_mark_sent(UID, "spb", "2026-10-02 18:05:00"))
    assert first is True
    assert second is False
    got, _confirmed = _run(db.checkin_qr_send_counts())
    assert got == 1
