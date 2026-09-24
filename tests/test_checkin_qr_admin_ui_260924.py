"""Форум-ночь п.3 (D-03, идея №2): хендлеры раздела «✅ Отметки на форуме» вокруг рассылки
QR — «📤 Разослать QR сейчас» (с подтверждением), «⚙️ Настройки QR» (тумблер + оба времени).
Бизнес-правила (аудитория/идемпотентность/планирование) уже покрыты
`tests/test_checkin_qr_broadcast_260924.py` — здесь только сам хендлер: парсинг callback_data,
права, перерисовка экрана, реальная постановка джобы через сохранённый настройками путь.

Стиль фикстур — `tests/test_admin_checkin_260924.py` (`_FakeCallback`/`_FakeMessage`,
`asyncio.run`, БД — шаблонная копия `tests/_dbtpl.py::fast_init_db`) + `tests/
test_ambassador_wave_scheduling_32.py` (реальный `AsyncIOScheduler` на временном jobstore —
`_safe_reschedule` реально зовёт `services.scheduler.get_scheduler()`, без него упал бы
`RuntimeError` — тест проверяет и то, что этого НЕ происходит)."""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from database.db import _connect
from handlers import admin_checkin
from handlers.states import CheckinQrTimeEdit
import services.scheduler as sched
from tests._dbtpl import fast_init_db

ADMIN_ID = 910202
UID = 260924201


def _db_ready(tmp_path, name="test_checkin_qr_admin_ui_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _insert_user(telegram_id, *, status="approved", season=None, event_city=None):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, f"Delegate {telegram_id}", status, season, event_city),
        )
        await conn.commit()


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeCallbackMessage:
    def __init__(self):
        self.sent = []       # [(text, reply_markup)] — .answer
        self.edited = []      # [(text, reply_markup)] — .edit_text

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None

    async def edit_text(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.edited.append((text, reply_markup))
        return None


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeCallbackMessage()
        self.answers = []  # [(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class _FakeMessage:
    def __init__(self, user_id, text=None):
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.sent = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class FakeBot:
    def __init__(self):
        self.photos = []

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


def _flat_cbs(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# «📤 Разослать QR сейчас» — превью счётчика + подтверждение
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_confirm_empty_pool_answers_alert_without_confirm_screen(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback("checkinqr_send:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_send_confirm(cb))
    assert cb.answers and cb.answers[0][1] is True  # show_alert=True
    assert cb.message.sent == []  # никакого экрана подтверждения


def test_send_confirm_nonempty_pool_shows_count_and_confirm_buttons(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(UID))
    cb = _FakeCallback("checkinqr_send:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_send_confirm(cb))
    assert cb.message.sent
    text, kb = cb.message.sent[0]
    assert "Уйдёт 1" in text
    cbs = _flat_cbs(kb)
    assert "checkinqr_send_go:_all" in cbs
    assert "checkinqr_send_no" in cbs


def test_send_cancel_edits_message():
    cb = _FakeCallback("checkinqr_send_no", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_send_cancel(cb))
    assert cb.message.edited
    assert "Отменено" in cb.message.edited[0][0]


def test_send_go_sends_photos_and_reports_counts(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(UID))
    asyncio.run(_insert_user(UID + 1))
    bot = _with_bot(monkeypatch)

    cb = _FakeCallback("checkinqr_send_go:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_send_go(cb))

    assert len(bot.photos) == 2
    assert cb.message.edited  # «⏳ Рассылаю QR...»
    final_text = cb.message.sent[-1][0]
    assert "2 доставлено" in final_text


# ══════════════════════════════════════════════════════════════════════════════════════════
# «⚙️ Настройки QR» — экран, тумблер, оба времени
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_cfg_screen_shows_forum_date_warning_when_unset(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback("checkinqr_cfg:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    text = cb.message.sent[0][0]
    assert "дата начала форума" in text.lower()


def test_cfg_screen_no_warning_when_forum_date_set(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("forum_date", "03.10.2026"))
    cb = _FakeCallback("checkinqr_cfg:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    text = cb.message.sent[0][0]
    assert "не задана" not in text


def test_toggle_go_flips_setting_and_reschedules_without_crashing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date", "03.10.2026"))

    async def body(s):
        # Дефолт "on" (без явной настройки) -> первый тап выключает per_city рассылку и снимает
        # уже стоящую джобу (реальная постановка джобы этим же путём проверена ниже).
        from services.checkin_broadcast import schedule_city_jobs
        await schedule_city_jobs(None)
        assert s.get_job("checkin_qr_evening:all") is not None

        cb = _FakeCallback("checkinqr_toggle:_all", ADMIN_ID)
        await admin_checkin.checkinqr_toggle_go(cb)
        assert cb.message.edited
        val = await db.get_setting("checkin_qr_broadcast_enabled")
        assert val == "off"
        assert s.get_job("checkin_qr_evening:all") is None  # снята вместе с выключением

    _run_scheduled(tmp_path, monkeypatch, body)


def test_toggle_go_is_fail_soft_without_scheduler(tmp_path):
    """`_safe_reschedule` — сбой планировщика (не поднят вовсе) не должен ронять сохранение
    тумблера, только залогироваться (services.scheduler._scheduler остаётся None по
    умолчанию в этом тесте — не поднимаем)."""
    _db_ready(tmp_path)
    cb = _FakeCallback("checkinqr_toggle:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_toggle_go(cb))
    assert cb.message.edited  # экран всё равно перерисован
    assert cb.answers


def test_time_start_sets_fsm_state_and_prompts_example():
    state = _new_state(ADMIN_ID)
    cb = _FakeCallback("checkinqr_time:evening:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_time_start(cb, state))
    assert asyncio.run(state.get_state()) == CheckinQrTimeEdit.waiting_value.state
    data = asyncio.run(state.get_data())
    assert data["checkinqr_time_key"] == "checkin_qr_broadcast_time"
    assert data["checkinqr_time_city"] is None
    assert "18:00" in cb.message.sent[0][0]


def test_time_step_rejects_bad_format_and_stays_helpful(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinQrTimeEdit.waiting_value))
    asyncio.run(state.update_data(checkinqr_time_key="checkin_qr_broadcast_time", checkinqr_time_city=None))

    message = _FakeMessage(ADMIN_ID, text="не время")
    asyncio.run(admin_checkin.checkinqr_time_step(message, state))
    assert any("формат" in (t or "").lower() or "чч:мм" in (t or "").lower() for t, _ in message.sent)


def test_time_step_saves_valid_value_and_reschedules(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date", "03.10.2026"))

    async def body(s):
        state = _new_state(ADMIN_ID)
        await state.set_state(CheckinQrTimeEdit.waiting_value)
        await state.update_data(
            checkinqr_time_key="checkin_qr_broadcast_time", checkinqr_time_city=None,
        )
        message = _FakeMessage(ADMIN_ID, text="19:30")
        await admin_checkin.checkinqr_time_step(message, state)
        val = await db.get_setting("checkin_qr_broadcast_time")
        assert val == "19:30"
        ev_job = s.get_job("checkin_qr_evening:all")
        assert ev_job is not None
        assert ev_job.next_run_time.replace(tzinfo=None).strftime("%H:%M") == "19:30"

    _run_scheduled(tmp_path, monkeypatch, body)


def test_time_step_scoped_to_city_when_cities_module_on(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    # "spb" — один из городов по умолчанию config.EVENT_CITIES (см. tests/test_admin_checkin_260924.py) —
    # ничего дополнительно заводить не нужно, только включить модуль.
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date__city__spb", "03.10.2026"))

    async def body(s):
        state = _new_state(ADMIN_ID)
        await state.set_state(CheckinQrTimeEdit.waiting_value)
        await state.update_data(
            checkinqr_time_key="checkin_qr_broadcast_time", checkinqr_time_city="spb",
        )
        message = _FakeMessage(ADMIN_ID, text="17:00")
        await admin_checkin.checkinqr_time_step(message, state)
        composed = await db.get_setting("checkin_qr_broadcast_time__city__spb")
        assert composed == "17:00"
        global_val = await db.get_setting("checkin_qr_broadcast_time")
        assert global_val is None  # глобальный ключ не тронут
        assert s.get_job("checkin_qr_evening:spb") is not None

    _run_scheduled(tmp_path, monkeypatch, body)
