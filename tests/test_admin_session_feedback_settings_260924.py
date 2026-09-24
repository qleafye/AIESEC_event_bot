"""Ревью 24.09 (аудит ключей после 8c0d8af): экран «⭐ Отзывы о сессиях» —
`handlers/session_feedback.py::render_feedback_settings_screen` + хендлеры
`prog_fbset*/prog_fbtoggle/prog_fbdelay*/prog_fbtext*`. Пять ключей реестра
(`session_feedback_enabled`, `session_feedback_delay_minutes` + четыре текста) жили ТОЛЬКО в
Mini App (на проде выключен) — этот экран впервые делает их доступными в самом боте.

pytest-asyncio недоступна — async через `asyncio.run()`. Fake-объекты callback/message — та
же форма, что `tests/test_admin_program_260924.py`. БД — `tmp_path`, шаблон `fast_init_db`.
"""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
from handlers import session_feedback as sf_handlers
from handlers.admin_caps import role_caps_key
from handlers.states import EditSetting
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 900924101
BOUND_MSK_ID = 900924102
BOUND_SPB_ID = 900924103


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_admin_session_feedback_settings.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _setup_bound_staff():
    await db.set_setting(role_caps_key("reg_manager"), "settings")
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text=None, user_id=SUPERADMIN_ID):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.answer_markups = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=SUPERADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Экран: дефолты, вход из «🗓 Программа форума»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_screen_shows_default_off_and_default_delay(tmp_path):
    _ready(tmp_path)
    text, kb = _run(sf_handlers.render_feedback_settings_screen("msk"))
    assert "❌ Выкл" in text
    assert "10 мин" in text
    cbs = _cbs(kb)
    assert "prog_fbtoggle:msk" in cbs
    assert any(cb == "prog_fbdelay:msk:10" for cb in cbs)
    assert "prog_fbdelay_custom:msk" in cbs
    assert "prog_fbtext:prompt" in cbs
    assert "prog_fbtext:thanks" in cbs
    assert "prog_fbtext:hint" in cbs
    assert "prog_fbtext:saved" in cbs


def test_prog_fbset_button_present_on_city_program_screen(tmp_path):
    from handlers import admin_program
    _ready(tmp_path)
    text, kb = _run(admin_program.render_city_program_screen(SUPERADMIN_ID, cities.default_city_code()))
    assert any(t == "⭐ Отзывы о сессиях" for t in _texts(kb))


def test_prog_fbset_open_renders_settings_screen(tmp_path):
    _ready(tmp_path)
    code = cities.default_city_code()
    callback = _FakeCallback(f"prog_fbset:{code}", user_id=SUPERADMIN_ID)
    _run(sf_handlers.prog_fbset_open(callback))
    assert "Отзывы о сессиях" in callback.message.text_edited


def test_prog_fbset_open_wrong_city_denied_for_bound_manager(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    callback = _FakeCallback("prog_fbset:spb", user_id=BOUND_MSK_ID)
    _run(sf_handlers.prog_fbset_open(callback))
    assert callback.answers[0][1] is True  # show_alert
    assert callback.message.text_edited is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Тумблер per_city
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_toggle_module_off_writes_plain_global_key(tmp_path):
    _ready(tmp_path)
    code = cities.default_city_code()
    callback = _FakeCallback(f"prog_fbtoggle:{code}", user_id=SUPERADMIN_ID)
    _run(sf_handlers.prog_fbtoggle(callback))
    assert _run(db.get_setting("session_feedback_enabled")) == "on"
    assert _run(db.get_setting(cities.per_city_key("session_feedback_enabled", code))) is None
    assert "✅ Вкл" in callback.message.text_edited


def test_toggle_module_on_writes_composite_city_key_not_global(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    callback = _FakeCallback("prog_fbtoggle:msk", user_id=SUPERADMIN_ID)
    _run(sf_handlers.prog_fbtoggle(callback))
    assert _run(db.get_setting(cities.per_city_key("session_feedback_enabled", "msk"))) == "on"
    assert _run(db.get_setting("session_feedback_enabled")) is None
    # Другой город не затронут (per_city не течёт между городами).
    assert _run(db.get_setting(cities.per_city_key("session_feedback_enabled", "spb"))) is None


def test_toggle_retap_flips_back_off(tmp_path):
    _ready(tmp_path)
    code = cities.default_city_code()
    _run(sf_handlers.prog_fbtoggle(_FakeCallback(f"prog_fbtoggle:{code}", user_id=SUPERADMIN_ID)))
    _run(sf_handlers.prog_fbtoggle(_FakeCallback(f"prog_fbtoggle:{code}", user_id=SUPERADMIN_ID)))
    assert _run(db.get_setting("session_feedback_enabled")) == "off"


def test_toggle_denied_for_manager_bound_to_other_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    callback = _FakeCallback("prog_fbtoggle:spb", user_id=BOUND_MSK_ID)
    _run(sf_handlers.prog_fbtoggle(callback))
    assert callback.answers[0][1] is True
    assert _run(db.get_setting(cities.per_city_key("session_feedback_enabled", "spb"))) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пресеты задержки + «Другое»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_delay_preset_writes_value_and_marks_current(tmp_path):
    _ready(tmp_path)
    code = cities.default_city_code()
    callback = _FakeCallback(f"prog_fbdelay:{code}:30", user_id=SUPERADMIN_ID)
    _run(sf_handlers.prog_fbdelay(callback))
    assert _run(db.get_setting("session_feedback_delay_minutes")) == "30"
    assert "30 мин" in callback.message.text_edited
    _, kb = _run(sf_handlers.render_feedback_settings_screen(code))
    assert any(t == "• 30 мин" for t in _texts(kb))


def test_delay_custom_start_enters_fsm_with_correct_key(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_fbdelay_custom:msk", user_id=SUPERADMIN_ID)
    _run(sf_handlers.prog_fbdelay_custom_start(callback, state))
    assert _run(state.get_state()) == EditSetting.waiting_for_value.state
    data = _run(state.get_data())
    assert data["setting_key"] == cities.per_city_key("session_feedback_delay_minutes", "msk")


def test_delay_custom_invalid_input_gives_clear_error_and_stays_in_state(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    code = cities.default_city_code()
    _run(sf_handlers.prog_fbdelay_custom_start(_FakeCallback(f"prog_fbdelay_custom:{code}"), state))
    msg = _FakeMessage(text="не число", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert any("целое число" in a for a in msg.answers_sent)
    assert _run(state.get_state()) == EditSetting.waiting_for_value.state  # не вышли из состояния
    assert _run(db.get_setting("session_feedback_delay_minutes")) is None


def test_delay_custom_valid_input_saves_via_generic_editor(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    code = cities.default_city_code()
    _run(sf_handlers.prog_fbdelay_custom_start(_FakeCallback(f"prog_fbdelay_custom:{code}"), state))
    msg = _FakeMessage(text="25", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting("session_feedback_delay_minutes")) == "25"
    assert _run(state.get_state()) is None


def test_delay_custom_reconciles_existing_session_job(tmp_path, monkeypatch):
    """Задача плана: смена задержки переставляет уже стоящую джобу отзыва (не только
    будущие сессии) — реальный `AsyncIOScheduler`, тот же харнесс, что
    `tests/test_session_feedback_260924.py`."""
    from handlers import admin_settings
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    import services.scheduler as sched
    from services import session_feedback as sf

    _ready(tmp_path)
    code = cities.default_city_code()
    now = msk_now()
    from datetime import timedelta
    end = now + timedelta(minutes=30)
    sid = _run(db.create_program_session(
        code, now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), end.strftime("%H:%M"), "Сессия",
    ))

    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.sqlite'}")},
        timezone=sched.MOSCOW_TZ,
    )
    monkeypatch.setattr(sched, "_scheduler", scheduler)

    async def go():
        scheduler.start(paused=True)
        try:
            await sf.schedule_for_session(sid)
            first_run = scheduler.get_job(sf.feedback_job_id(sid)).trigger.run_date

            state = _new_state(SUPERADMIN_ID)
            await sf_handlers.prog_fbdelay_custom_start(
                _FakeCallback(f"prog_fbdelay_custom:{code}"), state,
            )
            msg = _FakeMessage(text="45", user_id=SUPERADMIN_ID)
            await admin_settings.settings_edit_value(msg, state)

            jobs = [j for j in scheduler.get_jobs() if j.id == sf.feedback_job_id(sid)]
            assert len(jobs) == 1
            assert jobs[0].trigger.run_date != first_run
        finally:
            scheduler.shutdown(wait=False)

    asyncio.run(go())


# ══════════════════════════════════════════════════════════════════════════════════════════
# Тексты — глобальные, общий EditSetting
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_text_edit_unknown_field_denied(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("prog_fbtext:bogus", user_id=SUPERADMIN_ID)
    state = _new_state(SUPERADMIN_ID)
    _run(sf_handlers.prog_fbtext_edit(callback, state))
    assert callback.answers[0][1] is True


def test_text_edit_prompt_enters_fsm_with_global_key(tmp_path):
    state = _new_state(SUPERADMIN_ID)
    _ready(tmp_path)
    callback = _FakeCallback("prog_fbtext:prompt", user_id=SUPERADMIN_ID)
    _run(sf_handlers.prog_fbtext_edit(callback, state))
    data = _run(state.get_data())
    assert data["setting_key"] == "session_feedback_prompt_text"
    assert _run(state.get_state()) == EditSetting.waiting_for_value.state


def test_text_edit_saves_through_generic_editor_and_delegate_reads_it(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(sf_handlers.prog_fbtext_edit(_FakeCallback("prog_fbtext:thanks"), state))
    msg = _FakeMessage(text="Спасибо большое!", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting("session_feedback_thanks_text")) == "Спасибо большое!"
