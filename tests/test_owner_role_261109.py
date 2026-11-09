"""Роль «👑 Второй владелец» (`owner`): полные права админки из бота, без записи в .env.

Суперадмин по-прежнему только `config.ADMIN_IDS`: его нельзя снять из бота, и он держит все
права даже без строки в staff. Второй владелец — обычная роль: выключенная не даёт ничего,
город фильтруется как у остальных сотрудников, техническим алертам в ADMIN_IDS она не нужна.
"""
import asyncio

from aiogram.dispatcher.event.bases import UNHANDLED

from config import config
from database import db
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import dispatch_callback, dispatch_message


ADMIN_ID = 902801
OWNER_ID = 902802
ROLE = "owner"
LABEL = "👑 Второй владелец"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_owner_role.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_owner_not_in_admin_ids_gets_every_capability(tmp_path):
    _ready(tmp_path)
    from handlers.access.admin_caps import ALL_CAPABILITIES, resolve_capabilities, capability_holders

    assert OWNER_ID not in config.ADMIN_IDS
    asyncio.run(db.add_staff(OWNER_ID, ROLE, ADMIN_ID))
    assert asyncio.run(resolve_capabilities(OWNER_ID)) == set(ALL_CAPABILITIES)
    holders = asyncio.run(capability_holders("settings"))
    assert OWNER_ID in holders and ADMIN_ID in holders


def test_disabled_owner_role_grants_nothing(tmp_path):
    _ready(tmp_path)
    from handlers.access.admin_caps import resolve_capabilities, capability_holders

    asyncio.run(db.add_staff(OWNER_ID, ROLE, ADMIN_ID))
    asyncio.run(db.set_setting("role_owner_enabled", "off"))
    assert asyncio.run(resolve_capabilities(OWNER_ID)) == set()
    assert OWNER_ID not in asyncio.run(capability_holders("settings"))


def test_role_default_is_all_capabilities_not_a_second_list(tmp_path):
    _ready(tmp_path)
    from handlers.access.admin_caps import ALL_CAPABILITIES, ROLES
    from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed

    assert ROLES[ROLE]["default_caps"] == list(ALL_CAPABILITIES)
    assert list(SETTINGS_SCHEMA["role_caps_owner"]["default"]) == list(ALL_CAPABILITIES)
    assert asyncio.run(get_setting_typed("role_caps_owner")) == list(ALL_CAPABILITIES)
    assert asyncio.run(get_setting_typed("role_owner_enabled")) == "on"
    assert SETTINGS_SCHEMA["role_caps_owner"]["group"] == "roles"
    assert SETTINGS_SCHEMA["role_owner_enabled"]["group"] == "roles"


def test_role_is_offered_and_removed_from_roles_screen(tmp_path):
    _ready(tmp_path)
    result, event = dispatch_message(str(OWNER_ID), ADMIN_ID, raw_state="StaffAdd:waiting_for_person")
    assert result is not UNHANDLED
    picker = _buttons(event.answers[-1][2])
    button = next(b for b in picker if b.callback_data == f"roles_addrole:{OWNER_ID}:{ROLE}")
    assert button.text == LABEL
    assert ROLE not in button.text

    result, _ = dispatch_callback(f"roles_addrole:{OWNER_ID}:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    assert ROLE in asyncio.run(db.get_staff_roles(OWNER_ID))

    from handlers.access import admin_roles
    text = asyncio.run(admin_roles.render_roles_text())
    assert LABEL in text
    assert ROLE not in text
    kb = _flat(asyncio.run(admin_roles.build_roles_keyboard(ADMIN_ID)))
    assert f"roles_toggle:{ROLE}" in kb and f"roles_caps:{ROLE}" in kb
    assert f"roles_del:{OWNER_ID}:{ROLE}" in kb

    result, _ = dispatch_callback(f"roles_del_ok:{OWNER_ID}:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    assert ROLE not in asyncio.run(db.get_staff_roles(OWNER_ID))
    from handlers.access.admin_caps import resolve_capabilities
    assert asyncio.run(resolve_capabilities(OWNER_ID)) == set()


def test_caps_screen_is_checkboxes_not_codes(tmp_path):
    _ready(tmp_path)
    from handlers.access.admin_caps import ALL_CAPABILITIES, CAP_LABELS

    result, event = dispatch_callback(f"roles_caps:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    texts = [b.text for row in event.message.markup.inline_keyboard for b in row]
    for cap in ALL_CAPABILITIES:
        assert f"✅ {CAP_LABELS[cap]}" in texts
        assert cap not in " ".join(texts)


def test_bootstrap_admin_cannot_be_removed_and_keeps_all_caps(tmp_path):
    _ready(tmp_path)
    from handlers.access.admin_caps import ALL_CAPABILITIES, resolve_capabilities

    assert asyncio.run(db.get_staff_roles(ADMIN_ID)) == []
    assert asyncio.run(resolve_capabilities(ADMIN_ID)) == set(ALL_CAPABILITIES)

    result, event = dispatch_callback(f"roles_del:{ADMIN_ID}:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    assert event.answers == [("Это суперадмин из .env, снять из бота нельзя", True)]
    result, event = dispatch_callback(f"roles_del_ok:{ADMIN_ID}:{ROLE}", ADMIN_ID)
    assert event.answers == [("Это суперадмин из .env, снять из бота нельзя", True)]
    assert asyncio.run(db.get_staff_roles(ADMIN_ID)) == []
    assert ADMIN_ID in config.ADMIN_IDS
    assert asyncio.run(resolve_capabilities(ADMIN_ID)) == set(ALL_CAPABILITIES)


def test_owner_city_filters_like_staff_superadmin_does_not(tmp_path):
    _ready(tmp_path)
    from handlers.access.admin_caps import capability_holders

    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(OWNER_ID, ROLE, ADMIN_ID))
    asyncio.run(db.set_staff_city(OWNER_ID, "msk"))

    holders = asyncio.run(capability_holders("settings", city="spb"))
    assert ADMIN_ID in holders
    assert OWNER_ID not in holders

    asyncio.run(db.set_staff_city(OWNER_ID, None))
    holders = asyncio.run(capability_holders("settings", city="spb"))
    assert OWNER_ID in holders  # пустая привязка = все города, как у обычного staff


def test_settings_guide_describes_role_without_codes():
    from handlers.access.admin_caps import ALL_CAPABILITIES
    from handlers.access.admin_roles import SETTINGS_GUIDE_KEYS, _render_guide_entry

    assert {"role_caps_owner", "role_owner_enabled"} <= set(SETTINGS_GUIDE_KEYS)
    from handlers.access.admin_roles import SETTINGS_GUIDE_SECTIONS

    entries = [
        entry
        for _title, _sub, block in SETTINGS_GUIDE_SECTIONS
        for entry in block
        if entry["key"] in ("role_caps_owner", "role_owner_enabled")
    ]
    text = "\n".join(_render_guide_entry(entry, None) for entry in entries)
    assert "Второй владелец" in text
    assert "галочк" in text
    assert ROLE not in text
    for cap in ALL_CAPABILITIES:
        assert cap not in text


def test_dashboard_and_miniapp_keep_role_when_default_is_filled(tmp_path):
    from dashboard import access as dash_access
    from dashboard import db as dash_db
    from miniapp import deps
    from services.access.miniapp_access import BOT_ONLY_CAPS

    _ready(tmp_path)
    asyncio.run(db.add_staff(OWNER_ID, ROLE, ADMIN_ID))
    assert dash_access._ROLE_DEFAULT_CAPS[ROLE] == list(dash_access.ALL_CAPABILITIES)
    assert dash_access._ROLE_DEFAULT_CAPS[ROLE]  # пустой дефолт обнулил бы роль в приложении

    with dash_db.read_conn(config.DB_PATH) as conn:
        caps = dash_access.resolve_capabilities(conn, OWNER_ID, (ADMIN_ID,))
        assert caps == set(dash_access.ALL_CAPABILITIES)
        assert dash_access.viewer_scope(conn, OWNER_ID, (ADMIN_ID,), "spb").city == "spb"

    request = type("R", (), {})()
    request.app = type("A", (), {})()
    request.app.state = type("S", (), {})()
    request.app.state.cfg = type("C", (), {})()
    request.app.state.cfg.db_path = config.DB_PATH
    request.app.state.cfg.admin_ids = (ADMIN_ID,)
    request.app.state.cfg.bot_token = "x"
    request.scope = {"session": {}}
    request.session = {"telegram_id": OWNER_ID}
    request.method = "GET"
    request.headers = {}
    request.state = type("St", (), {})()

    principal = deps.principal(request, None)
    assert principal.telegram_id == OWNER_ID
    assert principal.caps == frozenset(dash_access.ALL_CAPABILITIES) - BOT_ONLY_CAPS
    assert "stats" in principal.caps and "settings" in principal.caps
