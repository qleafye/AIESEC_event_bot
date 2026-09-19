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
