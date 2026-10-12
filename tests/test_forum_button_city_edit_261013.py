"""Правка городской настройки с экрана, где город пришёл кнопкой (хаб форума при шапке
«🌍 Все города»): дедлайн записи, тексты записи, тексты теста, задержка отзыва.

Приёмка 13.10: при шапке «🌍 Все города» хаб спрашивает город, экран «📅 Настройки записи»
несёт его в callback (`prog_enrdl:msk`), а сохранение сверяло город ключа с шапкой — и падало
с «Город админки изменился», значение не писалось. Защита от смены шапки посреди правки при
этом должна остаться: переключил шапку в другом окне — правка отменяется, ничего не пишется."""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from database import db
from domain.cities import ALL_CITIES, per_city_key, set_admin_city
from handlers.forum import admin_enroll_list, admin_quiz_levels, session_feedback
from handlers.settings import admin_settings
from services.forum.session_enroll import ENROLL_TEXT_KEYS
from tests._enroll38 import ADMIN_ID, ready, run

MOVED = "Город админки изменился — начните правку заново."


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.html_text = text
        self.from_user = _User(ADMIN_ID)
        self.sent = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kw):
        self.sent.append(text)

    async def edit_text(self, text=None, parse_mode=None, reply_markup=None, **kw):
        self.sent.append(text)


class _CB:
    def __init__(self, data):
        self.data = data
        self.from_user = _User(ADMIN_ID)
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


def _setup(tmp_path):
    ready(tmp_path, "button_city_261013.db")
    run(db.set_setting("event_city_enabled", "on"))


def _flow(entry, cb_data, value, header, *, switch_to=None):
    """Открыть правку кнопкой при шапке `header`, (опц.) переключить шапку и прислать значение."""
    run(set_admin_city(ADMIN_ID, header))
    state = _state()
    cb = _CB(cb_data)
    run(entry(cb, state))
    assert not any(alert for _t, alert in cb.answers), cb.answers  # вход не отказал
    if switch_to is not None:
        run(set_admin_city(ADMIN_ID, switch_to))
    msg = _Msg(value)
    run(admin_settings.settings_edit_value(msg, state))
    return msg.sent


def _stored(base, code="msk"):
    return run(db.get_setting(per_city_key(base, code)))


# ── «⏳ Закрыть запись» (prog_enrdl) ─────────────────────────────────────────────────────────

def test_deadline_saves_when_header_is_all_cities(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_enroll_list.prog_enrdl, "prog_enrdl:msk", "01.10.2026 00:00", ALL_CITIES)
    assert MOVED not in sent, sent
    assert _stored("session_enroll_deadline") is not None


def test_deadline_saves_when_header_is_same_city(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_enroll_list.prog_enrdl, "prog_enrdl:msk", "01.10.2026 00:00", "msk")
    assert MOVED not in sent, sent
    assert _stored("session_enroll_deadline") is not None


def test_deadline_refused_when_header_switched_mid_edit(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_enroll_list.prog_enrdl, "prog_enrdl:msk", "01.10.2026 00:00", ALL_CITIES,
                 switch_to="spb")
    assert MOVED in sent, sent
    assert _stored("session_enroll_deadline") is None


def test_deadline_refused_when_city_header_switched_to_all(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_enroll_list.prog_enrdl, "prog_enrdl:msk", "01.10.2026 00:00", "msk",
                 switch_to=ALL_CITIES)
    assert MOVED in sent, sent
    assert _stored("session_enroll_deadline") is None


# ── «✏️ Тексты» записи (prog_enrte) ──────────────────────────────────────────────────────────

def test_enroll_text_saves_when_header_is_all_cities(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_enroll_list.prog_enrte, "prog_enrte:msk:0", "Выберите сессии", ALL_CITIES)
    assert MOVED not in sent, sent
    assert _stored(ENROLL_TEXT_KEYS[0]) == "Выберите сессии"


def test_enroll_text_refused_when_header_switched_mid_edit(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_enroll_list.prog_enrte, "prog_enrte:msk:0", "Выберите сессии", ALL_CITIES,
                 switch_to="spb")
    assert MOVED in sent, sent
    assert _stored(ENROLL_TEXT_KEYS[0]) is None


# ── Тексты теста компетенций (prog_qzte) ─────────────────────────────────────────────────────

def _points_idx():
    return admin_quiz_levels._EDIT_KEYS.index("quiz_points_max")


def test_quiz_text_saves_when_header_is_all_cities(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_quiz_levels.prog_qzte, f"prog_qzte:msk:{_points_idx()}", "7", ALL_CITIES)
    assert MOVED not in sent, sent
    assert _stored("quiz_points_max") == "7"


def test_quiz_text_saves_when_header_is_same_city(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_quiz_levels.prog_qzte, f"prog_qzte:msk:{_points_idx()}", "7", "msk")
    assert MOVED not in sent, sent
    assert _stored("quiz_points_max") == "7"


def test_quiz_text_refused_when_header_switched_mid_edit(tmp_path):
    _setup(tmp_path)
    sent = _flow(admin_quiz_levels.prog_qzte, f"prog_qzte:msk:{_points_idx()}", "7", ALL_CITIES,
                 switch_to="spb")
    assert MOVED in sent, sent
    assert _stored("quiz_points_max") is None


# ── «Своё число» задержки отзыва о сессии (prog_fbdelay_custom) ──────────────────────────────

def test_feedback_delay_saves_when_header_is_all_cities(tmp_path):
    _setup(tmp_path)
    sent = _flow(session_feedback.prog_fbdelay_custom_start, "prog_fbdelay_custom:msk", "25", ALL_CITIES)
    assert MOVED not in sent, sent
    assert _stored("session_feedback_delay_minutes") == "25"


def test_feedback_delay_refused_when_header_switched_mid_edit(tmp_path):
    _setup(tmp_path)
    sent = _flow(session_feedback.prog_fbdelay_custom_start, "prog_fbdelay_custom:msk", "25", ALL_CITIES,
                 switch_to="spb")
    assert MOVED in sent, sent
    assert _stored("session_feedback_delay_minutes") is None
