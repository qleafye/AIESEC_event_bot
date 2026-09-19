"""Quick 260919 (ln7) — третья роль «📊 Менеджер статистики» (`stats_manager`).

Зачем роль: менеджеру, который ведёт только цифры, раньше приходилось выдавать
`reg_manager`/`game_manager` с лишними правами или править их `role_caps_*`, ломая настоящих
модераторов. Роль с единственным правом `stats` закрывает это без расширения модели прав.

Почему хватает ОДНОЙ записи в `ROLES` (D-07, `handlers/admin_caps.py`): форма ролей
data-driven — `resolve_capabilities`, экран «👥 Роли и доступы» (`render_roles_text`,
`build_roles_keyboard`, `roles_add`) и раздел «📊 Данные» уже итерируют `ROLES`/`ALL_CAPABILITIES`
и не содержат захардкоженных под две роли мест. Новая роль появляется в UI сама — этот файл
фиксирует контракт, а не добавляет новое поведение сверху.
"""
import asyncio

from config import config
from database import db


ADMIN_ID = 900901
MANAGER_ID = 900902
NEW_ID = 900903
STATS_ROLE = "stats_manager"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_stats_manager_role.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


# ═══════════════════════════════════════════════════════════════════════════════════════
# Задача 1 — модель прав: ROLES, два ключа реестра, дефолт дашборда
# ═══════════════════════════════════════════════════════════════════════════════════════

def test_stats_manager_capabilities_is_exactly_stats(tmp_path):
    _ready(tmp_path)
    from handlers import admin_caps

    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))
    caps = asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID))
    assert caps == {"stats"}


def test_stats_manager_disabled_role_grants_nothing(tmp_path):
    _ready(tmp_path)
    from handlers import admin_caps

    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))
    asyncio.run(db.set_setting("role_stats_manager_enabled", "off"))
    caps = asyncio.run(admin_caps.resolve_capabilities(MANAGER_ID))
    assert caps == set()


def test_role_caps_stats_manager_default_is_registry_default(tmp_path):
    _ready(tmp_path)
    from settings_schema import get_setting_typed

    value = asyncio.run(get_setting_typed("role_caps_stats_manager"))
    assert value == ["stats"]


def test_capability_holders_stats_includes_stats_manager(tmp_path):
    _ready(tmp_path)
    from handlers import admin_caps

    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))
    stats_holders = asyncio.run(admin_caps.capability_holders("stats"))
    reg_holders = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    assert MANAGER_ID in stats_holders
    assert MANAGER_ID not in reg_holders


def test_stats_manager_registry_keys_are_in_roles_group(tmp_path):
    from settings_schema import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA["role_caps_stats_manager"]["group"] == "roles"
    assert SETTINGS_SCHEMA["role_stats_manager_enabled"]["group"] == "roles"


def test_stats_manager_registry_keys_not_editable(tmp_path):
    import settings_ops

    assert "role_caps_stats_manager" not in settings_ops.editable_keys()
    assert "role_stats_manager_enabled" not in settings_ops.editable_keys()


def test_dashboard_role_defaults_do_not_drift_from_bot():
    from handlers.admin_caps import ROLES
    from dashboard.access import _ROLE_DEFAULT_CAPS

    assert set(_ROLE_DEFAULT_CAPS) == set(ROLES)
    for role in ROLES:
        assert _ROLE_DEFAULT_CAPS[role] == ROLES[role]["default_caps"]


def test_dashboard_has_stats_true_for_stats_manager(tmp_path):
    from dashboard import access as dash_access
    from dashboard import db as dash_db

    path = str(tmp_path / "access.db")
    config.DB_PATH = path
    asyncio.run(db.init_db())
    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))

    with dash_db.read_conn(path) as conn:
        assert dash_access.has_stats(conn, MANAGER_ID, (ADMIN_ID,)) is True


def test_dashboard_has_stats_false_when_role_disabled(tmp_path):
    from dashboard import access as dash_access
    from dashboard import db as dash_db

    path = str(tmp_path / "access.db")
    config.DB_PATH = path
    asyncio.run(db.init_db())
    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))
    asyncio.run(db.set_setting("role_stats_manager_enabled", "off"))

    with dash_db.read_conn(path) as conn:
        assert dash_access.has_stats(conn, MANAGER_ID, (ADMIN_ID,)) is False


# ═══════════════════════════════════════════════════════════════════════════════════════
# Задача 2 — UI и доки: экран ролей, справка по настройкам, меню
# ═══════════════════════════════════════════════════════════════════════════════════════

def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_roles_keyboard_has_toggle_and_caps_buttons_for_stats_manager(tmp_path):
    _ready(tmp_path)
    from handlers import admin_roles

    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))
    kb = asyncio.run(admin_roles.build_roles_keyboard())
    cds = _flat_callback_data(kb)
    assert f"roles_toggle:{STATS_ROLE}" in cds
    assert f"roles_caps:{STATS_ROLE}" in cds


def test_add_manager_role_picker_offers_stats_manager(tmp_path):
    from tests.test_roles_phase8 import dispatch_message
    from aiogram.dispatcher.event.bases import UNHANDLED

    _ready(tmp_path)
    result, event = dispatch_message(
        str(NEW_ID), ADMIN_ID, raw_state="StaffAdd:waiting_for_person"
    )
    assert result is not UNHANDLED
    markup = event.answers[-1][2]
    cds = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"roles_addrole:{NEW_ID}:{STATS_ROLE}" in cds


def test_roles_addrole_stats_manager_creates_staff_with_all_cities(tmp_path):
    from tests.test_roles_phase8 import dispatch_callback
    from aiogram.dispatcher.event.bases import UNHANDLED

    _ready(tmp_path)
    result, _ = dispatch_callback(f"roles_addrole:{NEW_ID}:{STATS_ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    roles = asyncio.run(db.get_staff_roles(NEW_ID))
    assert STATS_ROLE in roles
    assert asyncio.run(db.get_staff_city(NEW_ID)) is None


def test_roles_toggle_stats_manager_flips_enabled_setting(tmp_path):
    from tests.test_roles_phase8 import dispatch_callback
    from aiogram.dispatcher.event.bases import UNHANDLED
    from settings_schema import get_setting_typed

    _ready(tmp_path)
    result, _ = dispatch_callback(f"roles_toggle:{STATS_ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    assert asyncio.run(get_setting_typed("role_stats_manager_enabled")) == "off"


def test_render_roles_text_shows_stats_manager_label_and_cap_label(tmp_path):
    from handlers import admin_roles
    from handlers.admin_caps import ROLES

    _ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, STATS_ROLE, ADMIN_ID))
    text = asyncio.run(admin_roles.render_roles_text())
    assert ROLES[STATS_ROLE]["label"] in text
    assert "📊 Статистика" in text


def test_stats_manager_sees_only_data_section_with_five_ops():
    from handlers.admin_sections import visible_sections, visible_rows

    caps = {"stats"}
    assert visible_sections(caps, False) == [("data", "📊 Данные")]
    rows = visible_rows("data", caps, False)
    ops = [value for kind, value in rows if kind == "op"]
    assert ops == [
        "admin_stats",
        "admin_monthly_stats",
        "admin_source_stats",
        "admin_export_csv",
        "admin_export_incomplete",
    ]


def test_settings_guide_knows_both_stats_manager_keys():
    from handlers.admin_roles import SETTINGS_GUIDE_KEYS

    assert "role_caps_stats_manager" in SETTINGS_GUIDE_KEYS
    assert "role_stats_manager_enabled" in SETTINGS_GUIDE_KEYS


def test_settings_guide_renders_label_not_raw_key():
    from handlers.admin_roles import (
        SETTINGS_GUIDE_SECTIONS,
        SETTINGS_GUIDE_KEYS,
        _render_settings_guide,
    )
    from handlers.admin_caps import ROLES

    chunks = _render_settings_guide(
        SETTINGS_GUIDE_SECTIONS, {k: None for k in SETTINGS_GUIDE_KEYS}
    )
    text = "\n".join(chunks)
    assert ROLES[STATS_ROLE]["label"] in text
    assert "role_caps_stats_manager" not in text
    assert STATS_ROLE not in text
