"""Общая настройка, открытая при городе в шапке: новое значение получат все города, поэтому
экран правки это говорит, а ввод начинается только с кнопки «✏️ Изменить для всех городов» —
случайное сообщение при просмотре экрана ничего не меняет."""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from database import db
from handlers import admin_settings as st
from handlers import admin_settings_global as gscope
from handlers.states import EditSetting
from tests.test_admin_sections_ia20 import FakeCallback, _enable_cities
from tests.test_roles_phase8 import ADMIN_ID, _flat_callback_data, _roles_ready

KEY = "miniapp_open_button"  # общая (не городская) настройка из группы «📱 Приложение: тексты в чате»


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.html_text = text
        self.from_user = type("U", (), {"id": ADMIN_ID})()
        self.answers = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


def _with_city_header(tmp_path):
    _roles_ready(tmp_path)
    _enable_cities()
    code = cities.city_codes()[1]
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, code))
    return code


def test_global_key_with_city_header_warns_and_waits_for_the_button(tmp_path):
    _with_city_header(tmp_path)
    state = _state()
    cb = FakeCallback(f"settings_edit:{KEY}")
    asyncio.run(st.settings_edit_start(cb, state))
    assert "Общая настройка (одна на все города)" in cb.message.text
    assert "Новое значение получат все города" in cb.message.text
    assert f"settings_edit_all:{KEY}" in _flat_callback_data(cb.message.markup)
    assert asyncio.run(state.get_state()) is None  # без кнопки ввод не ждём

    go = FakeCallback(f"settings_edit_all:{KEY}")
    asyncio.run(gscope.settings_edit_all(go, state))
    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_value.state
    asyncio.run(st.settings_edit_value(FakeMessage("📱 Открыть"), state))
    assert asyncio.run(db.get_setting(KEY)) == "📱 Открыть"


def test_all_cities_header_or_cities_off_edits_as_before(tmp_path):
    _roles_ready(tmp_path)  # города выключены
    state = _state()
    cb = FakeCallback(f"settings_edit:{KEY}")
    asyncio.run(st.settings_edit_start(cb, state))
    assert "Новое значение получат все города" not in cb.message.text
    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_value.state


def test_per_city_key_keeps_its_own_city_flow(tmp_path):
    _with_city_header(tmp_path)
    state = _state()
    cb = FakeCallback("settings_edit:miniapp_form_ambassador_offer_heading_text")  # городская
    asyncio.run(st.settings_edit_start(cb, state))
    assert "Новое значение получат все города" not in cb.message.text
    assert not any(c.startswith("settings_edit_all:") for c in _flat_callback_data(cb.message.markup) if c)


def test_stale_button_for_a_city_key_is_refused(tmp_path):
    _with_city_header(tmp_path)
    state = _state()
    cb = FakeCallback("settings_edit_all:miniapp_form_ambassador_offer_heading_text")
    asyncio.run(gscope.settings_edit_all(cb, state))
    assert cb.answers and cb.answers[0][1] is True
    assert asyncio.run(state.get_state()) is None
