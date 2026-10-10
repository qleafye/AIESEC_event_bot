"""Менеджер, привязанный к городу, не меняет общую настройку (одну на все города) — ни кнопкой
«✏️ Изменить для всех городов», ни старым вводом в общем редакторе, ни кнопкой варианта, ни
списком, ни из приложения. Проверяется на записи: старая клавиатура в чате живёт вечно.
Суперадмин и менеджер без привязки пишут общее, как раньше; свой город привязанный правит."""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
import domain.settings.ops as settings_ops
from database import db
from handlers import admin_settings as st
from handlers import admin_settings_enum as se
from handlers import admin_settings_global as gscope
from handlers import admin_settings_lists as sl
from handlers.states import EditSetting
from tests.test_admin_sections_ia20 import FakeCallback, _enable_cities
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID, _flat_callback_data, _roles_ready

COMMON_KEY = "miniapp_open_button"  # общая настройка
CITY_KEY = "miniapp_form_ambassador_offer_heading_text"  # городская
ENUM_KEY = "event_type"


class FakeMessage:
    def __init__(self, text, uid):
        self.text = text
        self.html_text = text
        self.from_user = type("U", (), {"id": uid})()
        self.answers = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kw):
        self.answers.append(text)


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _bound_manager(tmp_path, bound=True):
    _roles_ready(tmp_path)
    _enable_cities()
    code = cities.city_codes()[1]
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    if bound:
        assert asyncio.run(db.set_staff_city(MANAGER_ID, code))
    assert asyncio.run(cities.set_admin_city(MANAGER_ID, code))
    return code


def test_bound_manager_sees_why_and_gets_no_button_or_input(tmp_path):
    _bound_manager(tmp_path)
    state = _state(MANAGER_ID)
    cb = FakeCallback(f"settings_edit:{COMMON_KEY}", user_id=MANAGER_ID)
    asyncio.run(st.settings_edit_start(cb, state))
    assert settings_ops.COMMON_DENIED_TEXT.split(" —")[0] in cb.message.text
    assert f"settings_edit_all:{COMMON_KEY}" not in _flat_callback_data(cb.message.markup)
    assert asyncio.run(state.get_state()) is None


def test_bound_manager_cannot_write_through_the_all_cities_button(tmp_path):
    _bound_manager(tmp_path)
    state = _state(MANAGER_ID)
    cb = FakeCallback(f"settings_edit_all:{COMMON_KEY}", user_id=MANAGER_ID)
    asyncio.run(gscope.settings_edit_all(cb, state))
    assert cb.answers and cb.answers[0][1] is True
    assert asyncio.run(state.get_state()) is None


def test_bound_manager_cannot_write_with_an_old_input_or_enum_button(tmp_path):
    """Ввод, начатый до привязки (или со старой клавиатуры), упирается в запись."""
    _bound_manager(tmp_path)
    state = _state(MANAGER_ID)
    asyncio.run(state.set_state(EditSetting.waiting_for_value))
    asyncio.run(state.set_data({"setting_key": COMMON_KEY}))
    msg = FakeMessage("Чужой текст", MANAGER_ID)
    asyncio.run(st.settings_edit_value(msg, state))
    assert asyncio.run(db.get_setting(COMMON_KEY)) is None
    assert msg.answers == [settings_ops.COMMON_DENIED_TEXT]
    assert asyncio.run(state.get_state()) is None

    state = _state(MANAGER_ID)
    asyncio.run(state.set_state(EditSetting.waiting_for_value))
    asyncio.run(state.set_data({"setting_key": ENUM_KEY}))
    before = asyncio.run(db.get_setting(ENUM_KEY))

    class _Msg:  # кнопка варианта перекладывает выбор в settings_edit_value через model_copy
        def model_copy(self, update):
            return type("P", (), {"as_": lambda _s, _b: FakeMessage(update["text"], MANAGER_ID)})()

    pick = FakeCallback("settings_enum_pick:1", user_id=MANAGER_ID)
    pick.message = _Msg()
    pick.bot = None
    asyncio.run(se.settings_enum_pick(pick, state))
    assert asyncio.run(db.get_setting(ENUM_KEY)) == before


def test_bound_manager_cannot_change_a_common_list(tmp_path):
    _bound_manager(tmp_path)
    cb = FakeCallback("settings_list_add:source_options", user_id=MANAGER_ID)
    asyncio.run(sl.settings_list_add_start(cb, _state(MANAGER_ID)))
    assert cb.answers == [(settings_ops.COMMON_DENIED_TEXT, True)]


def test_bound_manager_still_edits_own_city_value(tmp_path):
    code = _bound_manager(tmp_path)
    state = _state(MANAGER_ID)
    asyncio.run(st.settings_edit_city(FakeCallback(f"settings_edit_city:{CITY_KEY}", user_id=MANAGER_ID), state))
    asyncio.run(st.settings_edit_value(FakeMessage("Своя ссылка Питера", MANAGER_ID), state))
    assert asyncio.run(db.get_setting(cities.per_city_key(CITY_KEY, code))) == "Своя ссылка Питера"
    assert asyncio.run(db.get_setting(CITY_KEY)) is None


def test_unbound_manager_and_superadmin_write_common_as_before(tmp_path):
    _bound_manager(tmp_path, bound=False)
    for uid, value in ((MANAGER_ID, "Открыть"), (ADMIN_ID, "📱 Открыть")):
        asyncio.run(cities.set_admin_city(uid, cities.city_codes()[1]))
        state = _state(uid)
        asyncio.run(gscope.settings_edit_all(FakeCallback(f"settings_edit_all:{COMMON_KEY}", user_id=uid), state))
        asyncio.run(st.settings_edit_value(FakeMessage(value, uid), state))
        assert asyncio.run(db.get_setting(COMMON_KEY)) == value


def test_mini_app_refuses_common_value_for_bound_manager(tmp_path):
    code = _bound_manager(tmp_path)
    check = asyncio.run(settings_ops.validate_batch_item(
        COMMON_KEY, "Чужой текст", visible_codes=[code], selected_city=code, cities_on=True,
    ))
    assert check.error == settings_ops.COMMON_DENIED_TEXT
    own = asyncio.run(settings_ops.validate_batch_item(
        cities.per_city_key(CITY_KEY, code), "Своё", visible_codes=[code], selected_city=code, cities_on=True,
    ))
    assert own.error is None
