"""Админка вуза: предупреждение при пустом списке и скрытые кнопки в «Анкете 2.0»."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.settings import admin_sections as sec
from handlers.settings import admin_settings
from tests._dbtpl import fast_init_db
from tests.test_settings_groups_c0x import ADMIN_ID, FakeCallback, _flat_callback_data


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "uni_admin.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_enabling_list_mode_with_empty_list_warns(tmp_path):
    _ready(tmp_path)

    async def go():
        cb = FakeCallback("toggle_uni_mode")
        await admin_settings.toggle_uni_mode(cb)
        return cb, await db.get_setting("reg_university_mode")

    cb, mode = asyncio.run(go())
    assert mode == "list"
    text, alert = cb.answers[-1]
    assert alert and "Список вузов пуст" in text and "Список ВУЗов" in text
    assert cb.message.edit_calls == 1


def test_enabling_list_mode_with_filled_list_has_no_warning(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("university_options", "МГУ")
        cb = FakeCallback("toggle_uni_mode")
        await admin_settings.toggle_uni_mode(cb)
        return cb

    cb = asyncio.run(go())
    assert "пуст" not in (cb.answers[-1][0] or "")


def test_v2_hides_university_buttons_in_bot(tmp_path):
    _ready(tmp_path)

    async def go(v2: str):
        await db.set_setting("reg_form_v2_enabled", v2)
        section = await sec.build_section_keyboard("form", ADMIN_ID)
        group = await admin_settings.build_settings_group_keyboard("reg", ADMIN_ID)
        return section, group

    section, group = asyncio.run(go("off"))
    assert "toggle_uni_mode" in _flat_callback_data(section)
    assert "settings_edit:university_options" in _flat_callback_data(group)

    section, group = asyncio.run(go("on"))
    assert "toggle_uni_mode" not in _flat_callback_data(section)
    assert "settings_edit:university_options" not in _flat_callback_data(group)
    note = admin_settings.UNI_V2_NOTE
    assert note in _texts(section) and note in _texts(group)
