"""Зашитые сроки → настройки реестра (бэклог «🛠», P1, ночь 10.10). Дефолт каждой настройки —
прежнее поведение; новое значение действует без перезапуска бота.

Реальный AsyncIOScheduler на временном jobstore — приём `tests/test_ambassador_wave_scheduling_32.py`.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from services import scheduler as sched
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db
from tests.test_ambassador_wave_scheduling_32 import _run_scheduled


def _ready(tmp_path, name="timings.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


# ── Напоминание о дедлайне задания: за сколько часов ─────────────────────────────────────

def test_deadline_reminder_hours_setting_default_is_old_behaviour():
    entry = SETTINGS_SCHEMA["wave_deadline_reminder_hours"]
    assert entry["type"] == "int" and entry["default"] == 24
    assert entry["min"] == 1 and entry["max"] == 168
    assert "24" in entry["prompt"]


def test_deadline_reminder_moves_when_setting_changes(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    deadline = (datetime.now() + timedelta(days=3)).replace(second=0, microsecond=0)
    task_id = _run(db.create_task("T", "Light", 10, "photo", deadline.strftime("%Y-%m-%d %H:%M:%S"), None))
    job_id = f"task_deadline_reminder_{task_id}"

    async def body(s):
        await sched.load_deadline_reminder_hours()
        await sched.reconcile_wave_jobs()
        first = s.get_job(job_id).trigger.run_date.replace(tzinfo=None)
        await db.set_setting("wave_deadline_reminder_hours", "3")
        await sched.on_setting_written("wave_deadline_reminder_hours")
        second = s.get_job(job_id).trigger.run_date.replace(tzinfo=None)
        return first, second

    first, second = _run_scheduled(tmp_path, monkeypatch, body)
    assert first == deadline - timedelta(hours=24)
    assert second == deadline - timedelta(hours=3)


def test_deadline_reminder_hours_bad_value_falls_back_to_24(tmp_path, monkeypatch):
    _ready(tmp_path, "timings_bad.db")
    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    _run(db.set_setting("wave_deadline_reminder_hours", "0"))
    assert _run(sched.load_deadline_reminder_hours()) == 24


# ── Вопрос «🔒 залип»: через сколько минут ───────────────────────────────────────────────

def test_question_stuck_threshold_follows_setting(tmp_path, monkeypatch):
    from services.comms import questions

    _ready(tmp_path, "timings_stuck.db")
    monkeypatch.setattr(questions, "_stuck_minutes", questions.STUCK_AFTER_MINUTES)
    now = datetime(2026, 10, 10, 12, 0, 0)
    row = {"answered_by": 1, "answered_at": (now - timedelta(minutes=45)).isoformat()}

    assert _run(questions.load_stuck_minutes()) == 30  # дефолт — прежние 30 минут
    assert questions.is_stuck(row, now=now) is True

    _run(db.set_setting("question_stuck_minutes", "60"))
    assert _run(questions.load_stuck_minutes()) == 60
    assert questions.is_stuck(row, now=now) is False

    _run(db.set_setting("question_stuck_minutes", "0"))
    assert _run(questions.load_stuck_minutes()) == 30  # мусор — дефолт, а не «залип сразу»


# ── QR: до скольки догонять утренний повтор ─────────────────────────────────────────────

def test_morning_catchup_until_follows_setting(tmp_path):
    from datetime import time
    from services import checkin_broadcast as cb

    _ready(tmp_path, "timings_qr.db")
    run_at = datetime(2026, 10, 10, 8, 0)
    at_13 = datetime(2026, 10, 10, 13, 0)

    until = _run(cb.morning_catchup_until(None))
    assert until == time(12, 0)  # дефолт — прежние 12:00
    assert cb.morning_catchup_ok(run_at, at_13, until) is False

    _run(db.set_setting("checkin_qr_morning_catchup_until", "15:00"))
    until = _run(cb.morning_catchup_until(None))
    assert until == time(15, 0)
    assert cb.morning_catchup_ok(run_at, at_13, until) is True

    _run(db.set_setting("checkin_qr_morning_catchup_until", "обед"))
    assert _run(cb.morning_catchup_until(None)) == time(12, 0)


# ── Ревью 10.10: ключи видны на экранах бота ─────────────────────────────────────────────

def test_new_keys_are_on_bot_screens_and_in_search():
    from handlers.settings import admin_settings
    from handlers.settings.admin_settings_search import candidates

    assert "question_stuck_minutes" in admin_settings._settings_group_keys("apps")
    assert "wave_deadline_reminder_hours" in admin_settings._settings_group_keys("amb")
    found = {c.key for c in candidates()}
    assert {"question_stuck_minutes", "wave_deadline_reminder_hours"} <= found


def test_qr_screen_has_catchup_row_and_rejects_time_before_morning_repeat(tmp_path):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import User
    from handlers.forum import admin_checkin
    from handlers.states import CheckinQrTimeEdit

    _ready(tmp_path, "timings_qr_screen.db")
    config.ADMIN_IDS = [77]
    text, kb = _run(admin_checkin._qr_cfg_text_kb(None))
    assert "догнать повтор до: 12:00" in text
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("checkinqr_time:catchup:") for cb in cbs)

    class _Msg:
        def __init__(self, text):
            self.text = text
            self.from_user = User(id=77, is_bot=False, first_name="Админ")
            self.sent = []

        async def answer(self, text, **kwargs):
            self.sent.append(text)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=77, user_id=77))

    async def go(value):
        await state.update_data(checkinqr_time_key="checkin_qr_morning_catchup_until", checkinqr_time_city=None)
        await state.set_state(CheckinQrTimeEdit.waiting_value)
        msg = _Msg(value)
        await admin_checkin.checkinqr_time_step(msg, state)
        return msg, await db.get_setting("checkin_qr_morning_catchup_until")

    msg, saved = _run(go("07:30"))
    assert saved is None and "догона не будет" in msg.sent[0]
    msg, saved = _run(go("14:00"))
    assert saved == "14:00"


# ── Ревью 10.10: переплан напоминания о дедлайне ─────────────────────────────────────────

def test_longer_lead_with_past_moment_drops_old_job(tmp_path, monkeypatch):
    _ready(tmp_path, "timings_drop.db")
    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    deadline = (datetime.now() + timedelta(hours=30)).replace(second=0, microsecond=0)
    task_id = _run(db.create_task("T", "Light", 10, "photo", deadline.strftime("%Y-%m-%d %H:%M:%S"), None))
    job_id = f"task_deadline_reminder_{task_id}"

    async def body(s):
        await sched.reconcile_wave_jobs()
        before = s.get_job(job_id) is not None
        await db.set_setting("wave_deadline_reminder_hours", "48")  # момент «за 48 ч» уже прошёл
        await sched.on_setting_written("wave_deadline_reminder_hours")
        return before, s.get_job(job_id)

    before, after = _run_scheduled(tmp_path, monkeypatch, body)
    assert before is True and after is None  # старая джоба не сработает по прежнему сроку


def test_reminder_is_sent_once_per_deadline(tmp_path, monkeypatch):
    from tests.test_ambassador_wave_scheduling_32 import FakeBot, _seed_user, _with_bot

    _ready(tmp_path, "timings_once.db")
    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    _seed_user(5001)
    _run(db.set_user_status(5001, "approved"))
    deadline = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    task_id = _run(db.create_task("T", "Light", 10, "photo", deadline, None))
    bot = _with_bot(monkeypatch, FakeBot())

    _run(sched.send_task_deadline_reminder(task_id))
    assert len(bot.sent) == 1
    # Срок напоминания уменьшили после отправки — сверка/повторный запуск второй раз не шлёт.
    _run(sched.send_task_deadline_reminder(task_id))
    assert len(bot.sent) == 1
    assert sched._already_reminded(_run(db.get_task(task_id)))

    # Новый срок задания — новое напоминание разрешено.
    new_deadline = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    _run(db.update_task_deadline(task_id, new_deadline))
    assert not sched._already_reminded(_run(db.get_task(task_id)))


def test_task_created_too_close_to_deadline_warns(tmp_path, monkeypatch):
    from handlers.game import game_task_wizard

    monkeypatch.setattr(sched, "_deadline_reminder_hours", 24)
    monkeypatch.setattr(game_task_wizard, "schedule_task_deadline_reminder", lambda *_a: False)
    soon = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    note = game_task_wizard._safe_schedule_reminder(1, soon)
    assert note and "меньше 24 ч" in note
    monkeypatch.setattr(game_task_wizard, "schedule_task_deadline_reminder", lambda *_a: True)
    assert game_task_wizard._safe_schedule_reminder(1, soon) is None
