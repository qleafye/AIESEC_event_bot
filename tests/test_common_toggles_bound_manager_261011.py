"""Тумблеры модулей (`_toggle_module_setting`) и кнопки-циклы вариантов (`_cycle_enum_setting`)
пишут общий ключ — один на все города. Менеджер, привязанный к городу, его не меняет: всплывает
объяснение, значение остаётся прежним. Суперадмин и менеджер без привязки переключают, как раньше;
городской вариант цикла привязанный по-прежнему правит для своего города."""
import asyncio

import domain.cities as cities
import domain.settings.ops as settings_ops
from database import db
from handlers.settings import admin_settings as st
from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_common_settings_bound_manager_261010 import _bound_manager
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID

TOGGLE_KEY = "nudge_enabled"  # общий тумблер модуля
ENUM_KEY = "reg_submit_notify_mode"  # общий цикл вариантов
CITY_ENUM_KEY = "reg_edit_policy"  # цикл вариантов с городским значением


def _denied(cb):
    return cb.answers == [(settings_ops.COMMON_DENIED_TEXT, True)]


def test_bound_manager_cannot_flip_a_module_toggle(tmp_path):
    _bound_manager(tmp_path)
    before = asyncio.run(db.get_setting(TOGGLE_KEY))
    cb = FakeCallback("toggle_nudge_enabled", user_id=MANAGER_ID)
    asyncio.run(st.toggle_nudge_enabled(cb))
    assert _denied(cb)
    assert asyncio.run(db.get_setting(TOGGLE_KEY)) == before
    assert cb.message.edit_calls == 0


def test_bound_manager_cannot_cycle_a_common_enum(tmp_path):
    _bound_manager(tmp_path)
    before = asyncio.run(db.get_setting(ENUM_KEY))
    cb = FakeCallback("toggle_reg_submit_notify", user_id=MANAGER_ID)
    asyncio.run(st.toggle_reg_submit_notify(cb))
    assert _denied(cb)
    assert asyncio.run(db.get_setting(ENUM_KEY)) == before


def test_bound_manager_still_cycles_own_city_enum(tmp_path):
    code = _bound_manager(tmp_path)
    cb = FakeCallback("toggle_reg_edit_policy", user_id=MANAGER_ID)
    asyncio.run(st._cycle_enum_setting(cb, CITY_ENUM_KEY, {}))
    assert asyncio.run(db.get_setting(cities.per_city_key(CITY_ENUM_KEY, code))) is not None
    assert asyncio.run(db.get_setting(CITY_ENUM_KEY)) is None


def test_unbound_manager_and_superadmin_flip_common_toggles(tmp_path):
    _bound_manager(tmp_path, bound=False)
    for uid in (MANAGER_ID, ADMIN_ID):
        before = asyncio.run(db.get_setting(TOGGLE_KEY))
        cb = FakeCallback("toggle_nudge_enabled", user_id=uid)
        asyncio.run(st.toggle_nudge_enabled(cb))
        assert asyncio.run(db.get_setting(TOGGLE_KEY)) != before
        assert not _denied(cb)

        before = asyncio.run(db.get_setting(ENUM_KEY))
        cb = FakeCallback("toggle_reg_submit_notify", user_id=uid)
        asyncio.run(st.toggle_reg_submit_notify(cb))
        assert asyncio.run(db.get_setting(ENUM_KEY)) != before
