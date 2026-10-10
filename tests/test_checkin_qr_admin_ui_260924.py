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
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from database.db import _connect
from handlers import admin_checkin
from handlers.admin_caps import role_caps_key, role_enabled_key
from handlers.states import CheckinQrTimeEdit
import services.checkin_broadcast as broadcast_svc
import services.scheduler as sched
from tests._dbtpl import fast_init_db

ADMIN_ID = 910202
MANAGER_ID = 910203
UID = 260924201


def _bind_manager_to_city(manager_id, city):
    """Тот же приём, что `tests/test_admin_percity_ui.py::_add_bound_manager` — менеджер,
    закреплённый ЗА ОДНИМ городом (`staff.city`), не суперадмин (не в `config.ADMIN_IDS`)."""
    asyncio.run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(manager_id, city))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))


def _db_ready(tmp_path, name="test_checkin_qr_admin_ui_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _forum_tomorrow():
    """Ручная «📤 Разослать QR сейчас» проверяет дату форума города — завтра подходит."""
    from datetime import timedelta
    from services.timeutil import msk_now
    day = (msk_now() + timedelta(days=1)).strftime("%d.%m.%Y")
    asyncio.run(db.set_setting("forum_date", day))


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
    _forum_tomorrow()
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
    _forum_tomorrow()
    asyncio.run(_insert_user(UID))
    asyncio.run(_insert_user(UID + 1))
    bot = _with_bot(monkeypatch)

    cb = _FakeCallback("checkinqr_send_go:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_send_go(cb))

    assert len(bot.photos) == 2
    assert cb.message.edited  # «⏳ Рассылаю QR...»
    final_text = cb.message.edited[-1][0]  # итог — на месте «⏳ Рассылаю QR...»
    assert "доставлено 2 из 2" in final_text


def test_send_go_clears_keyboard_before_sending(tmp_path, monkeypatch):
    """Находка ревью 260924 (п.4): клавиатура подтверждения убирается ДО запуска рассылки —
    повторный тап на неё физически невозможен."""
    _db_ready(tmp_path)
    _forum_tomorrow()
    asyncio.run(_insert_user(UID))
    _with_bot(monkeypatch)

    cb = _FakeCallback("checkinqr_send_go:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_send_go(cb))

    assert cb.message.edited
    edited_text, edited_markup = cb.message.edited[0]
    assert "рассылаю" in edited_text.lower()
    assert edited_markup is None


def test_send_go_rejects_concurrent_tap(tmp_path, monkeypatch):
    """Второй тап, пока первая рассылка того же города ещё держит лок, отвечает понятным
    текстом — не запускает вторую параллельную отправку."""
    _db_ready(tmp_path)
    _forum_tomorrow()
    asyncio.run(_insert_user(UID))
    bot = _with_bot(monkeypatch)

    async def body():
        lock = broadcast_svc._get_city_lock(None)
        async with lock:
            cb = _FakeCallback("checkinqr_send_go:_all", ADMIN_ID)
            await admin_checkin.checkinqr_send_go(cb)
            return cb

    cb = asyncio.run(body())
    assert bot.photos == []
    final_text = cb.message.edited[-1][0]  # итог — на месте «⏳ Рассылаю QR...»
    assert "уже идёт" in final_text.lower()


def test_send_go_ignores_quiet_hours(tmp_path, monkeypatch):
    """D-35 (24.09): QR — служебное сообщение, тихие часы на него больше НЕ действуют (раньше
    рассылка откладывалась до конца окна — владелец 24.09 явно это отменил)."""
    _db_ready(tmp_path)
    _forum_tomorrow()
    asyncio.run(_insert_user(UID))
    bot = _with_bot(monkeypatch)
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    # 23:00 накануне форума: дата форума выше берётся от реального «сегодня», поэтому и «сейчас»
    # считаем от него, а не фиксированной датой (иначе тест протухает, как только дата прошла).
    from services.timeutil import msk_now as real_msk_now
    evening = real_msk_now().replace(hour=23, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(broadcast_svc, "msk_now", lambda: evening)

    async def body(s):
        cb = _FakeCallback("checkinqr_send_go:_all", ADMIN_ID)
        await admin_checkin.checkinqr_send_go(cb)
        assert len(bot.photos) == 1
        final_text = cb.message.edited[-1][0]  # итог — на месте «⏳ Рассылаю QR...»
        assert "тихие часы" not in final_text.lower()
        assert "доставлено 1 из 1" in final_text

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# «⚙️ Настройки QR» — экран, тумблер, оба времени
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_cfg_screen_shows_qr_disabled_warning_by_default(tmp_path):
    """D-35 (24.09): master-тумблер `checkin_qr_enabled` по умолчанию выключен (дефолт "off") —
    экран настроек объясняет словами, почему рассылка не уйдёт, даже раньше проверки даты
    форума."""
    _db_ready(tmp_path)
    cb = _FakeCallback("checkinqr_cfg:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    text = cb.message.sent[0][0]
    assert "вход по qr выключен" in text.lower()


def test_cfg_screen_shows_forum_date_warning_when_unset(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    cb = _FakeCallback("checkinqr_cfg:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    text = cb.message.sent[0][0]
    assert "дата начала форума" in text.lower()


def test_cfg_screen_no_warning_when_forum_date_set(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date", "03.10.2037"))
    cb = _FakeCallback("checkinqr_cfg:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    text = cb.message.sent[0][0]
    assert "не задана" not in text
    assert "вход по qr выключен" not in text.lower()


def test_cfg_screen_no_quiet_hours_warning_at_all(tmp_path):
    """D-35 (24.09): тихие часы больше НЕ показываются на экране времени — QR служебное
    сообщение, время не может «увести» отправку."""
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date", "03.10.2037"))
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    asyncio.run(db.set_setting("checkin_qr_broadcast_time", "23:00"))
    cb = _FakeCallback("checkinqr_cfg:_all", ADMIN_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    text = cb.message.sent[0][0]
    assert "тихие часы" not in text.lower()


def test_toggle_go_flips_setting_and_reschedules_without_crashing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date", "03.10.2037"))

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
    asyncio.run(db.set_setting("forum_date", "03.10.2037"))

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
    asyncio.run(db.set_setting("forum_date__city__spb", "03.10.2037"))

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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Права: менеджер, закреплённый за ОДНИМ городом, не трогает чужой (тот же довод, что
# `handlers.admin_settings._cycle_enum_setting` для per-city ключей)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_bound_manager_denied_config_for_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _bind_manager_to_city(MANAGER_ID, "tyumen")

    cb = _FakeCallback("checkinqr_cfg:spb", MANAGER_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    assert cb.message.sent == []  # экран не показан
    assert cb.answers and cb.answers[0][1] is True  # show_alert=True


def test_bound_manager_denied_send_for_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _bind_manager_to_city(MANAGER_ID, "tyumen")
    asyncio.run(_insert_user(UID, event_city="spb"))

    cb = _FakeCallback("checkinqr_send:spb", MANAGER_ID)
    asyncio.run(admin_checkin.checkinqr_send_confirm(cb))
    assert cb.message.sent == []
    assert cb.answers and cb.answers[0][1] is True


def test_bound_manager_denied_toggle_for_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _bind_manager_to_city(MANAGER_ID, "tyumen")

    cb = _FakeCallback("checkinqr_toggle:spb", MANAGER_ID)
    asyncio.run(admin_checkin.checkinqr_toggle_go(cb))
    assert cb.message.edited == []
    assert cb.answers and cb.answers[0][1] is True


def test_bound_manager_allowed_config_for_own_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _bind_manager_to_city(MANAGER_ID, "spb")

    cb = _FakeCallback("checkinqr_cfg:spb", MANAGER_ID)
    asyncio.run(admin_checkin.checkinqr_cfg_screen(cb))
    assert cb.message.sent  # экран показан — свой город


# ══════════════════════════════════════════════════════════════════════════════════════════
# «Джоба переставляется при смене даты форума» — правка forum_date НЕМЕДЛЕННО (не после
# рестарта); settings_reschedule.reschedule_for_setting
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reschedule_hook_composite_key_touches_only_that_city(tmp_path, monkeypatch):
    from settings_reschedule import reschedule_for_setting as _reschedule_checkin_qr_if_forum_date

    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date__city__spb", "03.10.2037"))

    async def body(s):
        await _reschedule_checkin_qr_if_forum_date("forum_date__city__spb")
        assert s.get_job("checkin_qr_evening:spb") is not None
        assert s.get_job("checkin_qr_evening:tyumen") is None  # чужой город не тронут

    _run_scheduled(tmp_path, monkeypatch, body)


def test_reschedule_hook_bare_key_reconciles_every_city(tmp_path, monkeypatch):
    """Голый `forum_date` пересчитывает все города одним вызовом, но при включённом модуле
    городов общий ключ больше НЕ даёт дату городу без своей: иначе Москва получала QR за чужой
    региональный форум. spb (своя дата) стоит, tyumen/msk (только общая) — нет."""
    from settings_reschedule import reschedule_for_setting as _reschedule_checkin_qr_if_forum_date

    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date", "10.10.2037"))  # общий фолбэк — держит tyumen/msk
    asyncio.run(db.set_setting("forum_date__city__spb", "03.10.2037"))  # свой override

    async def body(s):
        await _reschedule_checkin_qr_if_forum_date("forum_date")
        assert s.get_job("checkin_qr_evening:spb") is not None
        assert s.get_job("checkin_qr_evening:tyumen") is None
        assert s.get_job("checkin_qr_evening:msk") is None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_reschedule_hook_ignores_unrelated_key(tmp_path, monkeypatch):
    from settings_reschedule import reschedule_for_setting as _reschedule_checkin_qr_if_forum_date

    _db_ready(tmp_path)

    async def body(s):
        await _reschedule_checkin_qr_if_forum_date("event_date")  # соседний ключ, не forum_date
        assert s.get_jobs() == []  # ничего не поставлено

    _run_scheduled(tmp_path, monkeypatch, body)


def test_forum_date_from_app_outbox_reschedules_jobs(tmp_path, monkeypatch):
    """Дата форума, сохранённая из приложения, доходит до бота только событием
    `settings_changed`; разборщик очереди обязан переставить QR-джобу сразу, не после рестарта."""
    from services import miniapp_outbox

    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("forum_date__city__spb", "03.10.2037"))

    async def body(s):
        assert s.get_job("checkin_qr_evening:spb") is None
        await miniapp_outbox._handle_row(
            None, "settings_changed", {"keys": ["forum_date__city__spb"], "by": 1},
        )
        assert s.get_job("checkin_qr_evening:spb") is not None

    _run_scheduled(tmp_path, monkeypatch, body)


def test_bot_save_reschedules_exactly_once(tmp_path, monkeypatch):
    """Запись из бота идёт через ту же воронку: переплан ровно один раз, не дважды."""
    import settings_reschedule
    from settings_audit import set_setting_by_admin

    _db_ready(tmp_path)
    calls = []
    real = settings_reschedule.reschedule_for_setting

    async def spy(key):
        calls.append(key)
        await real(key)

    monkeypatch.setattr(settings_reschedule, "reschedule_for_setting", spy)
    asyncio.run(set_setting_by_admin(ADMIN_ID, "forum_date", "03.10.2037"))
    assert calls == ["forum_date"]


def test_master_toggle_schedules_and_cancels_qr_and_volunteer_jobs(tmp_path, monkeypatch):
    """«🎟 Вход по QR» — от него зависит постановка джоб рассылки QR и шпаргалки волонтёру;
    раньше включение ничего не планировало до рестарта бота."""
    from handlers import admin_settings

    _db_ready(tmp_path)
    asyncio.run(db.set_setting("forum_date", "03.10.2037"))
    asyncio.run(db.set_setting("checkin_volunteer_guide_text", "🎫 Шпаргалка"))
    monkeypatch.setattr(broadcast_svc, "msk_now", lambda: datetime(2037, 9, 24, 12, 0))
    import services.checkin_volunteer_broadcast as vb
    monkeypatch.setattr(vb, "msk_now", lambda: datetime(2037, 9, 24, 12, 0))

    async def body(s):
        assert s.get_jobs() == []
        cb = _FakeCallback("toggle_checkin_qr_enabled", ADMIN_ID)
        await admin_settings.toggle_checkin_qr_enabled(cb)
        assert await db.get_setting("checkin_qr_enabled") == "on"
        assert s.get_job("checkin_qr_evening:all") is not None
        assert s.get_job("checkin_qr_morning:all") is not None
        assert s.get_job(vb.job_id(None)) is not None

        cb = _FakeCallback("toggle_checkin_qr_enabled", ADMIN_ID)
        await admin_settings.toggle_checkin_qr_enabled(cb)
        assert await db.get_setting("checkin_qr_enabled") == "off"
        assert s.get_jobs() == []

    _run_scheduled(tmp_path, monkeypatch, body)


def test_master_toggle_survives_reconcile_failure(tmp_path, monkeypatch):
    """Сверка упала — тумблер всё равно сохранён, менеджер получил подтверждение."""
    from handlers import admin_settings

    _db_ready(tmp_path)

    async def boom():
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(broadcast_svc, "reconcile_forum_jobs", boom)
    cb = _FakeCallback("toggle_checkin_qr_enabled", ADMIN_ID)
    asyncio.run(admin_settings.toggle_checkin_qr_enabled(cb))
    assert asyncio.run(db.get_setting("checkin_qr_enabled")) == "on"
    assert cb.answers and cb.message.edited
