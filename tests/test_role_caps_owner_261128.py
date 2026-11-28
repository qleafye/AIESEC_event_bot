"""Права роли пишет только владелец бота. Менеджер без города их больше не переключает."""
import asyncio

from database import db
from handlers.access import admin_roles
from handlers.access.admin_caps import role_caps_key
from tests.test_roles_bound_manager_261011 import _bound_manager
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID
from tests.test_admin_sections_ia20 import FakeCallback


def test_unbound_manager_cannot_change_role_caps(tmp_path):
    _bound_manager(tmp_path, bound=False)
    before = asyncio.run(db.get_setting(role_caps_key("reg_manager")))

    cb = FakeCallback("roles_cap:reg_manager:broadcast", user_id=MANAGER_ID)
    asyncio.run(admin_roles.toggle_role_cap(cb))

    assert cb.answers == [(admin_roles.ROLES_CAPS_OWNER_ONLY_TEXT, True)]
    assert admin_roles.ROLES_CAPS_OWNER_ONLY_TEXT == "Права ролей меняет только владелец бота."
    assert "без привязки к городу" not in cb.answers[0][0]
    assert asyncio.run(db.get_setting(role_caps_key("reg_manager"))) == before


def test_owner_still_toggles_role_cap(tmp_path):
    _bound_manager(tmp_path, bound=False)
    before = asyncio.run(db.get_setting(role_caps_key("reg_manager")))

    cb = FakeCallback("roles_cap:reg_manager:broadcast", user_id=ADMIN_ID)
    asyncio.run(admin_roles.toggle_role_cap(cb))

    assert cb.answers and cb.answers[0][0] != admin_roles.ROLES_CAPS_OWNER_ONLY_TEXT
    assert asyncio.run(db.get_setting(role_caps_key("reg_manager"))) != before


def test_bound_manager_still_gets_the_common_denial(tmp_path):
    _bound_manager(tmp_path)
    before = asyncio.run(db.get_setting(role_caps_key("reg_manager")))

    cb = FakeCallback("roles_cap:reg_manager:broadcast", user_id=MANAGER_ID)
    asyncio.run(admin_roles.toggle_role_cap(cb))

    assert cb.answers == [(admin_roles.ROLES_COMMON_DENIED_TEXT, True)]
    assert asyncio.run(db.get_setting(role_caps_key("reg_manager"))) == before
