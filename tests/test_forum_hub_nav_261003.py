"""Хаб «🎪 Форум: функции»: общий тумблер «🎟 Вход по QR» — только тому, кто видит все города."""
from __future__ import annotations

import asyncio

from database import db
from tests.test_admin_checkin_260924 import ADMIN_ID
from tests.test_forum_admin_uat_fixes_261003 import _CB, _kb_callbacks, _seed_spb

BOUND_ID = 905001
UNBOUND_ID = 905002


def _hub_cbs(uid: int, code: str = "spb") -> list[str]:
    from handlers import admin_forum_functions as aff
    _text, kb = asyncio.run(aff._render_hub(uid, code))
    return _kb_callbacks(kb)


def _staff(uid: int, city: str | None):
    async def go():
        # Менеджер с правом «Настройки» — иначе кнопку скрыла бы уже капа, а не город.
        await db.set_setting("role_caps_reg_manager", "moderate_reg\nsettings")
        await db.add_staff(uid, "reg_manager", ADMIN_ID)
        if city:
            await db.set_staff_city(uid, city)
    asyncio.run(go())


def test_superadmin_sees_shared_qr_button(tmp_path):
    _seed_spb(tmp_path)
    assert "forumfn_qr:spb" in _hub_cbs(ADMIN_ID)


def test_unbound_manager_sees_shared_qr_button(tmp_path):
    _seed_spb(tmp_path)
    _staff(UNBOUND_ID, None)
    assert "forumfn_qr:spb" in _hub_cbs(UNBOUND_ID)


def test_city_bound_manager_has_no_shared_qr_button_and_cannot_toggle(tmp_path):
    from handlers import admin_forum_hub_nav as nav
    _seed_spb(tmp_path)
    _staff(BOUND_ID, "spb")
    assert "forumfn_qr:spb" not in _hub_cbs(BOUND_ID)

    cb = _CB("forumfn_qr_set:off:spb", uid=BOUND_ID)
    asyncio.run(nav.forumfn_qr_set(cb))
    assert asyncio.run(db.get_setting("checkin_qr_enabled")) == "on"
    assert any("главный менеджер" in (a[0] or "") for a in cb.answers)
