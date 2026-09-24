"""Форум-ночь п.4 (расписание форума в боте, FORUM-CHECKIN.md D-18..D-20) — интерактивная
программа сессий (handlers/program.py) + гейт кнопки меню (keyboards/builders.py::get_main_menu_kb).

D-29: своей кнопки «🗓 Программа» больше нет — программа сессий это запасной вид объединённой
кнопки «📅 Программа форума» (`handlers/user_actions.py::show_program`), когда фото не
загружено. Экран делегата проверяется через show_program с отсутствующим фото.

pytest-asyncio недоступна — async через `asyncio.run()`, Fake-объекты — форма
`tests/test_faq_260906.py::_FakeMessage/_FakeCallback`. БД — tmp_path, шаблон через
`tests/_dbtpl.py::fast_init_db`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db
from handlers import program as program_handlers
from handlers import user_actions
from keyboards.builders import MENU_TEXTS, get_main_menu_kb
from tests._dbtpl import fast_init_db

DELEGATE_ID = 260924501


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_program_delegate.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _seed_delegate(uid=DELEGATE_ID, city=None, **extra):
    data = {"telegram_id": uid, "full_name": f"Delegate {uid}", "registration_date": "2026-09-06"}
    if city is not None:
        data["event_city"] = city
    data.update(extra)
    _run(db.add_user(data))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None
        self.username = None


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.chat = _FakeChat(user_id)
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
    def __init__(self, data, user_id=DELEGATE_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.chat = _FakeChat(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


PROGRAM_LABEL = "📅 Программа форума"


def _show_program(message, monkeypatch):
    """Объединённая кнопка без загруженного фото — ведёт в программу сессий."""
    def _no_photo(*a, **k):
        raise FileNotFoundError("resources/program.jpg")

    monkeypatch.setattr(user_actions, "FSInputFile", _no_photo)
    _run(user_actions.show_program(message))


def _flat_cb(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Гейт кнопки меню ─────────────────────────────────────────────────────────────────────────

def test_menu_hides_schedule_button_when_no_sessions(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    labels = [btn.text for row in kb.keyboard for btn in row]
    assert PROGRAM_LABEL not in labels


def test_menu_shows_schedule_button_when_sessions_exist(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    labels = [btn.text for row in kb.keyboard for btn in row]
    assert PROGRAM_LABEL in labels


def test_menu_gate_is_per_city(tmp_path):
    """Сессии есть у msk, но НЕ у spb -- у делегата spb кнопки быть не должно (даже когда
    модуль городов включён)."""
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _seed_delegate(city="spb")
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    labels = [btn.text for row in kb.keyboard for btn in row]
    assert PROGRAM_LABEL not in labels


# ── Экран делегата: один день / несколько дней ──────────────────────────────────────────────

def test_show_program_schedule_single_day_renders_directly(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:30", "Открытие форума"))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    assert len(message.answers_sent) == 1
    text = message.answers_sent[0]
    assert "Открытие форума" in text
    assert "10:00–11:30" in text
    assert "30.10.2026" in text


def test_show_program_schedule_multiple_days_shows_picker(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "День 1"))
    _run(db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "День 2"))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    text = message.answers_sent[0]
    kb = message.answer_markups[0]
    assert "Выберите день" in text
    cbs = _flat_cb(kb)
    assert cbs == ["pds_day:2026-10-30", "pds_day:2026-10-31"]


def test_show_program_schedule_not_registered_blocks(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    message = _FakeMessage(text=PROGRAM_LABEL, user_id=999999)
    _show_program(message, monkeypatch)
    assert "зарегистрироваться" in message.answers_sent[0]


def test_show_program_schedule_isolated_by_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие СПб"))
    _seed_delegate(city="msk")
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие Мск"))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    text = message.answers_sent[0]
    assert "Открытие Мск" in text
    assert "Открытие СПб" not in text


# ── Навигация между днями (callback) ─────────────────────────────────────────────────────────

def test_pds_day_open_renders_chosen_day(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "День 1"))
    _run(db.create_program_session("msk", "2026-10-31", "09:00", "10:00", "День 2"))
    callback = _FakeCallback("pds_day:2026-10-31")
    _run(program_handlers.pds_day_open(callback))
    assert "День 2" in callback.message.text_edited
    assert "День 1" not in callback.message.text_edited


def test_pds_days_back_returns_to_picker(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "День 1"))
    _run(db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "День 2"))
    # Пойти сначала на день, потом назад.
    day_cb = _FakeCallback("pds_day:2026-10-30")
    _run(program_handlers.pds_day_open(day_cb))
    back_cb = _FakeCallback("pds_days", message=day_cb.message)
    _run(program_handlers.pds_days_back(back_cb))
    assert "Выберите день" in day_cb.message.text_edited
    cbs = _flat_cb(day_cb.message.edit_markup)
    assert cbs == ["pds_day:2026-10-30", "pds_day:2026-10-31"]


# ── Параллельные сессии сгруппированы ────────────────────────────────────────────────────────

def test_parallel_sessions_grouped_in_day_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Сессия А"))
    _run(db.create_program_session("msk", "2026-10-30", "10:30", "11:30", "Сессия Б"))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    text = message.answers_sent[0]
    assert "Сессия А" in text
    assert "Сессия Б" in text
    assert "параллельно" in text.lower()


def test_non_overlapping_sessions_not_grouped(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Сессия А"))
    _run(db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "Сессия Б"))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    text = message.answers_sent[0]
    assert "параллельно" not in text.lower()


# ── «Идёт сейчас» / «Следующая» ──────────────────────────────────────────────────────────────

def test_now_and_next_markers_on_forum_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-30", "09:00", "10:00", "Уже прошла"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Идёт"))
    _run(db.create_program_session("msk", "2026-10-30", "11:00", "12:00", "Следующая сессия"))
    monkeypatch.setattr(program_handlers, "msk_now", lambda: datetime(2026, 10, 30, 10, 30))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    text = message.answers_sent[0]
    assert "Уже прошла" in text
    assert "🔴" in text and "Идёт сейчас" in text
    assert "⏭" in text and "Следующая" in text
    lines = text.splitlines()
    now_idx = next(i for i, line in enumerate(lines) if "Идёт сейчас" in line)
    running_idx = next(i for i, line in enumerate(lines) if line.endswith("— Идёт"))
    next_idx = next(i for i, line in enumerate(lines) if line.strip() == "⏭ Следующая")
    next_session_idx = next(i for i, line in enumerate(lines) if "Следующая сессия" in line)
    assert now_idx < running_idx
    assert next_idx < next_session_idx


def test_no_markers_on_non_forum_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "Сессия"))
    monkeypatch.setattr(program_handlers, "msk_now", lambda: datetime(2026, 10, 30, 10, 30))
    message = _FakeMessage(text=PROGRAM_LABEL)
    _show_program(message, monkeypatch)
    text = message.answers_sent[0]
    assert "Идёт сейчас" not in text
    assert "Следующая" not in text
