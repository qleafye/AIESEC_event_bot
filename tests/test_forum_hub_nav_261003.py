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


# ── «🔘 Кнопки меню» и «🎭 Оформление» из хаба: город хаба и возврат в хаб ─────────────────

class _State:
    async def clear(self):
        pass


def _open_from_hub(target: str, code: str = "spb", uid: int = ADMIN_ID):
    from handlers import admin_forum_hub_nav as nav
    cb = _CB(f"forumfn_open:{target}:{code}", uid=uid)
    asyncio.run(nav.forumfn_open(cb, _State()))
    return cb.message.edits[-1]


def test_menu_from_hub_uses_hub_city_not_header(tmp_path):
    from domain.cities import admin_selected_city, set_admin_city
    _seed_spb(tmp_path)
    asyncio.run(set_admin_city(ADMIN_ID, "msk"))
    text, _kb = _open_from_hub("menu", "spb")
    assert asyncio.run(admin_selected_city(ADMIN_ID)) == "spb"
    assert "Санкт-Петербург" in text


def test_menu_reset_city_keeps_way_back_to_hub(tmp_path):
    from handlers.regform import admin_reg_config
    _seed_spb(tmp_path)
    asyncio.run(db.set_setting("menu_info__city__spb", "off"))
    _t, menu_kb = _open_from_hub("menu", "spb")

    cb = _CB("menu_reset_city", markup=menu_kb)
    asyncio.run(admin_reg_config.menu_reset_city(cb))
    _text, confirm_kb = cb.message.edits[-1]
    assert "forumfn_open:menu:spb" in _kb_callbacks(confirm_kb)  # «Отмена» — на экран из хаба

    cb = _CB("menu_reset_city_go:spb", markup=confirm_kb)
    asyncio.run(admin_reg_config.menu_reset_city_go(cb))
    _text, after_kb = cb.message.edits[-1]
    assert after_kb.inline_keyboard[-1][0].callback_data == "forumfn_back:spb"


def test_theme_screen_from_hub_returns_to_hub_settings(tmp_path):
    from handlers import admin_miniapp_theme
    _seed_spb(tmp_path)
    _t, app_kb = _open_from_hub("app", "spb")
    cb = _CB("miniapp_theme_open", markup=app_kb)
    asyncio.run(admin_miniapp_theme.open_miniapp_theme(cb, _State()))
    theme_kb = cb.message.edits[-1][1]
    assert "forumfn_open:app:spb" in _kb_callbacks(theme_kb)
    assert "admin_miniapp_settings" not in _kb_callbacks(theme_kb)

    cb = _CB("miniapp_theme_toggle_playful", markup=theme_kb)
    asyncio.run(admin_miniapp_theme.miniapp_theme_toggle_playful(cb))
    assert "forumfn_open:app:spb" in _kb_callbacks(cb.message.edits[-1][1])


def test_theme_screen_from_section_keeps_native_back(tmp_path):
    from handlers import admin_miniapp_theme
    _seed_spb(tmp_path)
    cb = _CB("miniapp_theme_open")
    asyncio.run(admin_miniapp_theme.open_miniapp_theme(cb, _State()))
    assert "admin_miniapp_settings" in _kb_callbacks(cb.message.edits[-1][1])
