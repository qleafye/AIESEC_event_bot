"""Форум-ночь п.4 (расписание форума в боте, FORUM-CHECKIN.md D-18..D-20/D-24) — раздел
«🗓 Программа форума»: handlers/admin_program.py + handlers/admin_program_halls.py.

pytest-asyncio недоступна в этом окружении — async через `asyncio.run()`, Fake-объекты
callback/message — та же форма, что `tests/test_reject_rules_editor.py::_FakeCallback/
_FakeMessage`. БД — `tmp_path`, шаблон через `tests/_dbtpl.py::fast_init_db`.

Разделы:
- Экран города (закреплённый менеджер / модуль выключен / «Все города» -> выбор).
- Экран дня, мастер новой сессии (время -> название -> зал -> спикер -> описание).
- Конфликт зала (предупреждение + подтверждение, а не тихое сохранение).
- Точечная правка поля существующей сессии, удаление с подтверждением.
- Залы: список/переименование/удаление (с числом осиротевших сессий).
- Копирование программы дня между городами.
- Изоляция городов и права привязанного менеджера.
"""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from config import config
from database import db
from handlers import admin_program, admin_program_halls
from handlers.admin_caps import role_caps_key
from handlers.states import ProgramDayCustom, ProgramHallName, ProgramSessionField
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 900924001
BOUND_MSK_ID = 900924002
BOUND_SPB_ID = 900924003


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_admin_program.db"):
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


# ── Экран города: закреплённый / выключенный модуль / выбор ────────────────────────────────

def test_resolve_city_bound_manager_gets_own_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    code = _run(admin_program._resolve_city_for_screen(BOUND_MSK_ID))
    assert code == "msk"


def test_resolve_city_module_off_gets_default_city(tmp_path):
    _ready(tmp_path)
    code = _run(admin_program._resolve_city_for_screen(SUPERADMIN_ID))
    assert code == cities.default_city_code()


def test_resolve_city_all_cities_mode_returns_none(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    _run(cities.set_admin_city(SUPERADMIN_ID, cities.ALL_CITIES))
    code = _run(admin_program._resolve_city_for_screen(SUPERADMIN_ID))
    assert code is None


def test_admin_program_entry_all_cities_shows_picker(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    _run(cities.set_admin_city(SUPERADMIN_ID, cities.ALL_CITIES))
    callback = _FakeCallback("admin_program", user_id=SUPERADMIN_ID)
    _run(admin_program.admin_program_entry(callback))
    assert "Выберите город" in callback.message.text_edited
    cbs = _cbs(callback.message.edit_markup)
    assert any(cb and cb.startswith("prog_city:") for cb in cbs)


def test_admin_program_entry_bound_manager_opens_own_city_directly(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    callback = _FakeCallback("admin_program", user_id=BOUND_MSK_ID)
    _run(admin_program.admin_program_entry(callback))
    assert "Программа форума" in callback.message.text_edited
    assert "Выберите город" not in callback.message.text_edited


def test_prog_city_open_forbidden_for_bound_manager_of_other_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    callback = _FakeCallback("prog_city:spb", user_id=BOUND_MSK_ID)
    _run(admin_program.prog_city_open(callback))
    assert callback.answers and callback.answers[0][1] is True
    assert callback.message.text_edited is None


# ── Экран дня: подсказки дней из forum_date, «Другой день» ──────────────────────────────────

def test_render_city_program_screen_suggests_days_from_forum_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "30.10.2026"))
    _text, kb = _run(admin_program.render_city_program_screen(SUPERADMIN_ID, "msk"))
    cbs = _cbs(kb)
    assert "prog_day:msk:2026-10-30" in cbs
    assert "prog_day:msk:2026-10-31" in cbs
    assert "prog_day:msk:2026-11-01" not in cbs  # только дни форума (по умолчанию 2 дня)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("30.10.2026" in t for t in labels)


def test_render_city_program_screen_no_forum_date_no_days_but_has_add_day(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_program.render_city_program_screen(SUPERADMIN_ID, "msk"))
    assert "Дней пока нет" in text
    cbs = _cbs(kb)
    assert "prog_daynew:msk" in cbs


def test_render_city_program_screen_hides_copy_when_single_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(cities, "CITIES", [{"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0}])
    _text, kb = _run(admin_program.render_city_program_screen(SUPERADMIN_ID, "msk"))
    cbs = _cbs(kb)
    assert not any(cb and cb.startswith("prog_copy:") for cb in cbs)


def test_prog_daynew_custom_day_opens_day_screen(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_daynew:msk", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_daynew_start(callback, state))
    assert _run(state.get_state()) == ProgramDayCustom.value.state

    message = _FakeMessage(text="31.10", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_daynew_step(message, state))
    assert _run(state.get_state()) is None
    assert any("31.10.2026" in (t or "") for t in message.answers_sent)


def test_prog_daynew_bad_input_stays_in_state(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(state.set_data({"pd_city": "msk"}))
    _run(state.set_state(ProgramDayCustom.value))
    message = _FakeMessage(text="ерунда", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_daynew_step(message, state))
    assert _run(state.get_state()) == ProgramDayCustom.value.state
    assert "Не понял дату" in message.answers_sent[-1]


# ── Мастер новой сессии: полный путь без конфликта ──────────────────────────────────────────

async def _create_session_via_wizard(state, *, hall_choice="none", speaker="Пропустить", description="Пропустить"):
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    await admin_program.prog_new_start(callback, state)
    assert await state.get_state() == ProgramSessionField.time.state

    msg = _FakeMessage(text="10:00-11:30", user_id=SUPERADMIN_ID)
    await admin_program.prog_time_step(msg, state)
    assert await state.get_state() == ProgramSessionField.title.state

    msg = _FakeMessage(text="Открытие форума", user_id=SUPERADMIN_ID)
    await admin_program.prog_title_step(msg, state)
    assert await state.get_state() is None
    hall_text = msg.answers_sent[-1]
    hall_kb = msg.answer_markups[-1]

    hall_cb = _FakeCallback(f"prog_hp:w:{hall_choice}", user_id=SUPERADMIN_ID)
    await admin_program.prog_hp_pick(hall_cb, state)

    return hall_text, hall_kb, hall_cb


def test_wizard_creates_session_without_hall(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    hall_text, _hall_kb, hall_cb = _run(_create_session_via_wizard(state))
    assert "Зал" in hall_text
    # без конфликта -- сразу переходит к спикеру.
    assert _run(state.get_state()) == ProgramSessionField.speaker.state

    msg = _FakeMessage(text="Пропустить", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_speaker_step(msg, state))
    assert _run(state.get_state()) == ProgramSessionField.description.state

    msg = _FakeMessage(text="Первая сессия;новая строка", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_description_step(msg, state))
    assert _run(state.get_state()) is None

    sessions = _run(db.list_program_sessions_for_city_day("msk", "2026-10-30"))
    assert len(sessions) == 1
    s = sessions[0]
    assert s["title"] == "Открытие форума"
    assert s["start_time"] == "10:00"
    assert s["end_time"] == "11:30"
    assert s["hall_id"] is None
    assert s["speaker"] is None
    assert s["description"] == "Первая сессия\nновая строка"


def test_wizard_time_step_rejects_bad_input(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_new_start(callback, state))
    msg = _FakeMessage(text="не время", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_time_step(msg, state))
    assert _run(state.get_state()) == ProgramSessionField.time.state
    assert "Не понял время" in msg.answers_sent[-1]


def test_wizard_cancel_clears_state_and_creates_nothing(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_new_start(callback, state))
    msg = _FakeMessage(text="Отмена", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_field_cancel(msg, state))
    assert _run(state.get_state()) is None
    assert _run(state.get_data()) == {}
    assert _run(db.list_program_sessions_for_city_day("msk", "2026-10-30")) == []


def test_wizard_with_new_hall_created_on_the_fly(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_new_start(callback, state))
    _run(admin_program.prog_time_step(_FakeMessage(text="10:00-11:00", user_id=SUPERADMIN_ID), state))
    _run(admin_program.prog_title_step(_FakeMessage(text="Сессия", user_id=SUPERADMIN_ID), state))

    hallnew_cb = _FakeCallback("prog_hpnew:w", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hpnew_start(hallnew_cb, state))
    assert _run(state.get_state()) == ProgramHallName.value.state

    msg = _FakeMessage(text="Большой зал", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hallname_step(msg, state))
    assert _run(state.get_state()) == ProgramSessionField.speaker.state

    halls = _run(db.list_program_halls("msk"))
    assert len(halls) == 1
    assert halls[0]["name"] == "Большой зал"

    _run(admin_program.prog_speaker_step(_FakeMessage(text="-", user_id=SUPERADMIN_ID), state))
    _run(admin_program.prog_description_step(_FakeMessage(text="-", user_id=SUPERADMIN_ID), state))
    sessions = _run(db.list_program_sessions_for_city_day("msk", "2026-10-30"))
    assert sessions[0]["hall_id"] == halls[0]["id"]


# ── Конфликт зала: предупреждение + подтверждение ────────────────────────────────────────────

def test_wizard_hall_conflict_shows_confirm_and_does_not_save_until_confirmed(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Большой зал"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Уже стоит", hall_id=hall_id))

    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_new_start(callback, state))
    _run(admin_program.prog_time_step(_FakeMessage(text="10:30-11:30", user_id=SUPERADMIN_ID), state))
    _run(admin_program.prog_title_step(_FakeMessage(text="Новая сессия", user_id=SUPERADMIN_ID), state))

    hall_cb = _FakeCallback(f"prog_hp:w:{hall_id}", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hp_pick(hall_cb, state))
    assert "Большой зал" in hall_cb.message.text_edited
    assert "Уже стоит" in hall_cb.message.text_edited
    assert "Сохранить всё равно" in hall_cb.message.text_edited
    # Ещё НЕ ушли на шаг спикера -- значит сессия ещё не создаётся автоматически.
    assert _run(state.get_state()) != ProgramSessionField.speaker.state

    confirm_cb = _FakeCallback("prog_wconfirm_yes", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_wconfirm_yes(confirm_cb, state))
    assert _run(state.get_state()) == ProgramSessionField.speaker.state

    _run(admin_program.prog_speaker_step(_FakeMessage(text="Пропустить", user_id=SUPERADMIN_ID), state))
    _run(admin_program.prog_description_step(_FakeMessage(text="Пропустить", user_id=SUPERADMIN_ID), state))
    sessions = _run(db.list_program_sessions_for_city_day("msk", "2026-10-30"))
    assert len(sessions) == 2


def test_wizard_hall_conflict_no_reopens_hall_picker(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Большой зал"))
    _run(db.create_program_hall("msk", "Малый зал"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Уже стоит", hall_id=hall_id))

    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_new_start(callback, state))
    _run(admin_program.prog_time_step(_FakeMessage(text="10:30-11:30", user_id=SUPERADMIN_ID), state))
    _run(admin_program.prog_title_step(_FakeMessage(text="Новая сессия", user_id=SUPERADMIN_ID), state))
    hall_cb = _FakeCallback(f"prog_hp:w:{hall_id}", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hp_pick(hall_cb, state))

    no_cb = _FakeCallback("prog_wconfirm_no", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_wconfirm_no(no_cb, state))
    assert "Выберите зал" in no_cb.message.text_edited
    assert any(cb == "prog_hp:w:none" for cb in _cbs(no_cb.message.edit_markup))


# ── Правка одного поля существующей сессии ───────────────────────────────────────────────────

def test_field_edit_title_updates_immediately_without_wizard(tmp_path):
    _ready(tmp_path)
    session_id = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Старое"))
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback(f"prog_field:{session_id}:title", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_field_start(callback, state))
    assert _run(state.get_state()) == ProgramSessionField.title.state

    msg = _FakeMessage(text="Новое имя", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_title_step(msg, state))
    assert _run(state.get_state()) is None
    session = _run(db.get_program_session(session_id))
    assert session["title"] == "Новое имя"


def test_field_edit_time_conflict_requires_confirmation(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Большой зал"))
    other_id = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Соседняя", hall_id=hall_id))
    session_id = _run(db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "Правим", hall_id=hall_id))

    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback(f"prog_field:{session_id}:time", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_field_start(callback, state))
    msg = _FakeMessage(text="10:30-11:30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_time_step(msg, state))
    # Конфликт -- время НЕ применено, ждём подтверждения.
    session = _run(db.get_program_session(session_id))
    assert session["start_time"] == "12:00"
    assert any("Соседняя" in (t or "") for t in msg.answers_sent)

    confirm_cb = _FakeCallback(f"prog_ftyes:{session_id}", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_ftyes(confirm_cb, state))
    session = _run(db.get_program_session(session_id))
    assert session["start_time"] == "10:30"
    assert session["end_time"] == "11:30"
    assert other_id  # для ясности, что переменная использована


def test_field_edit_speaker_skip_clears_value(tmp_path):
    _ready(tmp_path)
    session_id = _run(db.create_program_session(
        "msk", "2026-10-30", "10:00", "11:00", "Сессия", speaker="Иван",
    ))
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback(f"prog_field:{session_id}:speaker", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_field_start(callback, state))
    msg = _FakeMessage(text="Пропустить", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_speaker_step(msg, state))
    session = _run(db.get_program_session(session_id))
    assert session["speaker"] is None


# ── Удаление сессии, с подтверждением ────────────────────────────────────────────────────────

def test_delete_session_confirm_names_title_and_time(tmp_path):
    _ready(tmp_path)
    session_id = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Убить"))
    callback = _FakeCallback(f"prog_d:{session_id}", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_delete_confirm(callback))
    assert "Убить" in callback.message.text_edited
    assert "10:00–11:00" in callback.message.text_edited
    # Не удалена ДО подтверждения.
    assert _run(db.get_program_session(session_id)) is not None


def test_delete_session_go_removes_it(tmp_path):
    _ready(tmp_path)
    session_id = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Убить"))
    callback = _FakeCallback(f"prog_dgo:{session_id}", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_delete_go(callback))
    assert _run(db.get_program_session(session_id)) is None
    assert "К программе" not in (callback.message.text_edited or "")  # это экран дня, не города


# ── Залы: список/переименование/удаление ─────────────────────────────────────────────────────

def test_halls_create_via_standalone_screen(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_hallcreate:msk", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_hallcreate_start(callback, state))
    assert _run(state.get_state()) == ProgramHallName.value.state
    msg = _FakeMessage(text="Малый зал", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hallname_step(msg, state))
    halls = _run(db.list_program_halls("msk"))
    assert [h["name"] for h in halls] == ["Малый зал"]


def test_halls_rename(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Старое"))
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback(f"prog_hallrename:{hall_id}", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_hallrename_start(callback, state))
    msg = _FakeMessage(text="Новое", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hallname_step(msg, state))
    hall = _run(db.get_program_hall(hall_id))
    assert hall["name"] == "Новое"


def test_halls_delete_confirm_mentions_orphaned_session_count(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Зал"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "S1", hall_id=hall_id))
    _run(db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "S2", hall_id=hall_id))
    callback = _FakeCallback(f"prog_halldel:{hall_id}", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_halldel_confirm(callback))
    assert "2 сессии" in callback.message.text_edited


def test_halls_delete_go_orphans_sessions(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Зал"))
    session_id = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "S1", hall_id=hall_id))
    callback = _FakeCallback(f"prog_halldelgo:{hall_id}", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_halldel_go(callback))
    assert _run(db.get_program_hall(hall_id)) is None
    session = _run(db.get_program_session(session_id))
    assert session is not None
    assert session["hall_id"] is None


def test_halls_rename_forbidden_for_bound_manager_of_other_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    hall_id = _run(db.create_program_hall("spb", "Зал СПб"))
    state = _new_state(BOUND_MSK_ID)
    callback = _FakeCallback(f"prog_hallrename:{hall_id}", user_id=BOUND_MSK_ID)
    _run(admin_program_halls.prog_hallrename_start(callback, state))
    assert callback.answers and callback.answers[0][1] is True
    assert _run(state.get_state()) is None


# ── Копирование программы между городами ─────────────────────────────────────────────────────

def test_copy_flow_end_to_end(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("spb", "Большой зал"))
    _run(db.create_program_session(
        "spb", "2026-10-03", "10:00", "11:00", "Открытие", hall_id=hall_id,
    ))

    src_cb = _FakeCallback("prog_copy:tyumen", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_copy_pick_source(src_cb))
    assert "prog_copysrc:tyumen:spb" in _cbs(src_cb.message.edit_markup)

    day_cb = _FakeCallback("prog_copysrc:tyumen:spb", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_copy_pick_day(day_cb))
    assert "prog_copyday:tyumen:spb:2026-10-03" in _cbs(day_cb.message.edit_markup)

    confirm_cb = _FakeCallback("prog_copyday:tyumen:spb:2026-10-03", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_copy_confirm(confirm_cb))
    assert "1 сессия" in confirm_cb.message.text_edited
    assert "не удаляется" in confirm_cb.message.text_edited

    go_cb = _FakeCallback("prog_copygo:tyumen:spb:2026-10-03", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_copy_go(go_cb))
    tyumen_sessions = _run(db.list_program_sessions_for_city_day("tyumen", "2026-10-03"))
    assert len(tyumen_sessions) == 1
    assert tyumen_sessions[0]["title"] == "Открытие"
    # Исходный город не тронут.
    spb_sessions = _run(db.list_program_sessions_for_city_day("spb", "2026-10-03"))
    assert len(spb_sessions) == 1


def test_copy_go_forbidden_for_bound_manager_of_other_destination_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    go_cb = _FakeCallback("prog_copygo:tyumen:spb:2026-10-03", user_id=BOUND_MSK_ID)
    _run(admin_program_halls.prog_copy_go(go_cb))
    assert go_cb.answers and go_cb.answers[0][1] is True
    assert _run(db.list_program_sessions_for_city_day("tyumen", "2026-10-03")) == []


# ── Карточка сессии: счётчик отметок «Отмечено: N (из вместимости)» (форум-ночь п.5) ────────

def test_session_card_shows_zero_checkins_without_hall(tmp_path):
    _ready(tmp_path)
    sid = _run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    text, _kb = _run(admin_program.render_session_card(sid))
    assert "Отмечено: 0" in text
    assert "из" not in text.split("Отмечено:")[1].splitlines()[0]


def test_session_card_shows_checkin_count_without_capacity(tmp_path):
    _ready(tmp_path)
    sid = _run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.record_session_checkin(1, sid, [], source="miniapp"))
    _run(db.record_session_checkin(2, sid, [], source="miniapp"))
    text, _kb = _run(admin_program.render_session_card(sid))
    assert "Отмечено: 2" in text


def test_session_card_shows_checkin_count_with_hall_capacity(tmp_path):
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Большой зал", capacity=120))
    sid = _run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие", hall_id=hall_id))
    _run(db.record_session_checkin(1, sid, [], source="miniapp"))
    text, _kb = _run(admin_program.render_session_card(sid))
    assert "Отмечено: 1 из 120" in text


def test_wizard_conflict_retime_and_inline_cancel(tmp_path):
    """Приёмка 03.10: на шагах «время»/«название» — инлайн-«Отмена»; при конфликте зала —
    «✏️ Ввести время заново», новое время проверяется на тот же зал, название не теряется."""
    _ready(tmp_path)
    hall_id = _run(db.create_program_hall("msk", "Большой зал"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Уже стоит", hall_id=hall_id))

    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("prog_new:msk:2026-10-30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_new_start(callback, state))
    assert "prog_wcancel" in _cbs(callback.message.answer_markups[-1])
    msg = _FakeMessage(text="10:30-11:30", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_time_step(msg, state))
    assert "prog_wcancel" in _cbs(msg.answer_markups[-1])
    _run(admin_program.prog_title_step(_FakeMessage(text="Новая сессия", user_id=SUPERADMIN_ID), state))
    hall_cb = _FakeCallback(f"prog_hp:w:{hall_id}", user_id=SUPERADMIN_ID)
    _run(admin_program.prog_hp_pick(hall_cb, state))
    assert "prog_wretime" in _cbs(hall_cb.message.edit_markup)

    re_cb = _FakeCallback("prog_wretime", user_id=SUPERADMIN_ID)
    _run(admin_program_halls.prog_wretime(re_cb, state))
    assert _run(state.get_state()) == ProgramSessionField.time.state
    _run(admin_program.prog_time_step(_FakeMessage(text="11:00-12:00", user_id=SUPERADMIN_ID), state))
    assert _run(state.get_state()) == ProgramSessionField.speaker.state  # конфликта больше нет
    _run(admin_program.prog_speaker_step(_FakeMessage(text="Пропустить", user_id=SUPERADMIN_ID), state))
    _run(admin_program.prog_description_step(_FakeMessage(text="Пропустить", user_id=SUPERADMIN_ID), state))
    new = [s for s in _run(db.list_program_sessions_for_city_day("msk", "2026-10-30")) if s["title"] == "Новая сессия"]
    assert new and new[0]["start_time"] == "11:00" and new[0]["hall_id"] == hall_id
