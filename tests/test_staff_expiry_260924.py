"""Идея №6 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): права со сроком
действия — общая механика, не только форум.

Покрытие: `staff.expires_at` (аддитивная миграция), `database.db.set_staff_expiry`,
фильтрация в `get_staff_roles`/`get_staff_ids_by_role` (D-6: «истёкшая роль не даёт НИКАКИХ
прав»), тот же контракт в `dashboard.access` (веб-дашборд И Mini App, который резолвит
capability через ЭТОТ ЖЕ модуль — `miniapp/deps.py`), экран «⏳ Срок действия роли»
(handlers/admin_roles.py).

pytest-asyncio недоступна — async через `asyncio.run()` (конвенция проекта, см. соседние
test_roles_phase8.py/test_dashboard_auth.py). БД — `tmp_path`, шаблон `fast_init_db`."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from dashboard import access as dash_access
from dashboard import db as dash_db
from handlers import admin_roles  # noqa: F401 -- регистрирует rexp:*/rexp_go:*/rexp_custom:*
from handlers.admin_caps import ROLES, resolve_capabilities
from services.staff_expiry import forum_end_date_iso, is_expiry_active, parse_ddmmyyyy, today_iso
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import _fresh_state, dispatch_callback, dispatch_message

ADMIN_ID = 924201
MANAGER_ID = 924210
STRANGER_ID = 924299


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="staff_expiry.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


# ── database/db.py: staff.expires_at, set_staff_expiry, фильтрация ─────────────────────────

def test_add_staff_default_expiry_is_unlimited(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    roles = _run(db.get_staff_roles(MANAGER_ID))
    assert roles == ["reg_manager"]
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] is None


def test_add_staff_with_expiry(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "volunteer", ADMIN_ID, expires_at="2026-10-04"))
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] == "2026-10-04"


def test_set_staff_expiry_changes_existing_role(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    ok = _run(db.set_staff_expiry(MANAGER_ID, "reg_manager", "2026-10-04"))
    assert ok is True
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] == "2026-10-04"


def test_set_staff_expiry_can_remove_expiry(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2026-10-04"))
    ok = _run(db.set_staff_expiry(MANAGER_ID, "reg_manager", None))
    assert ok is True
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] is None


def test_set_staff_expiry_returns_false_for_missing_role(tmp_path):
    _ready(tmp_path)
    ok = _run(db.set_staff_expiry(MANAGER_ID, "reg_manager", "2026-10-04"))
    assert ok is False


def test_add_staff_no_op_does_not_touch_existing_expiry():
    """Идея №5: «если у человека уже есть роль — не понижать» — повторный add_staff тем же
    role НЕ трогает уже выставленный срок, даже если вызван с другим expires_at."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        config.DB_PATH = f"{tmp}/staff_expiry_noop.db"
        fast_init_db()
        config.ADMIN_IDS = [ADMIN_ID]
        _run(db.add_staff(MANAGER_ID, "volunteer", ADMIN_ID, expires_at="2026-10-04"))
        created = _run(db.add_staff(MANAGER_ID, "volunteer", ADMIN_ID, expires_at="2099-01-01"))
        assert created is False
        staff = _run(db.list_staff())
        assert staff[0]["expires_at"] == "2026-10-04"


def test_get_staff_roles_excludes_expired_role(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2020-01-01"))
    assert _run(db.get_staff_roles(MANAGER_ID)) == []


def test_get_staff_roles_includes_role_expiring_today(tmp_path):
    _ready(tmp_path)
    today = today_iso()
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at=today))
    assert _run(db.get_staff_roles(MANAGER_ID)) == ["reg_manager"]


def test_get_staff_roles_includes_role_expiring_in_future(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2099-01-01"))
    assert _run(db.get_staff_roles(MANAGER_ID)) == ["reg_manager"]


def test_list_staff_keeps_expired_row_ie_nothing_deleted(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2020-01-01"))
    staff = _run(db.list_staff())
    assert len(staff) == 1
    assert staff[0]["telegram_id"] == MANAGER_ID


def test_get_staff_ids_by_role_excludes_expired(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2020-01-01"))
    other = MANAGER_ID + 1
    _run(db.add_staff(other, "reg_manager", ADMIN_ID))
    ids = _run(db.get_staff_ids_by_role("reg_manager"))
    assert ids == [other]


# ── handlers/admin_caps.py: истёкшая роль не даёт НИКАКИХ прав в боте ──────────────────────

def test_expired_role_grants_no_capabilities_in_bot(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2020-01-01"))
    caps = _run(resolve_capabilities(MANAGER_ID))
    assert caps == set()


def test_active_role_with_expiry_still_grants_capabilities(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2099-01-01"))
    caps = _run(resolve_capabilities(MANAGER_ID))
    assert "moderate_reg" in caps


def test_expiry_never_affects_bootstrap_superadmin(tmp_path):
    """D-12: ADMIN_IDS short-circuit никогда не смотрит на `staff` — expiry этой ветки не
    касается (superadmin не заведён в staff вовсе)."""
    _ready(tmp_path)
    caps = _run(resolve_capabilities(ADMIN_ID))
    assert "settings" in caps


# ── dashboard/access.py: тот же контракт для веб-дашборда И Mini App ───────────────────────

def _seed_access_db(tmp_path, name="staff_expiry_access.db", *, expires_at=None):
    path = str(tmp_path / name)
    config.DB_PATH = path
    fast_init_db()

    async def _fill():
        await db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at=expires_at)

    _run(_fill())
    return path


def test_dashboard_access_denies_expired_role(tmp_path):
    path = _seed_access_db(tmp_path, expires_at="2020-01-01")
    with dash_db.read_conn(path) as conn:
        caps = dash_access.resolve_capabilities(conn, MANAGER_ID, (ADMIN_ID,))
        assert caps == set()
        assert dash_access.has_stats(conn, MANAGER_ID, (ADMIN_ID,)) is False


def test_dashboard_access_allows_active_role_with_future_expiry(tmp_path):
    path = _seed_access_db(tmp_path, expires_at="2099-01-01")
    _run(db.set_setting("role_caps_reg_manager", "moderate_reg\nstats"))
    with dash_db.read_conn(path) as conn:
        caps = dash_access.resolve_capabilities(conn, MANAGER_ID, (ADMIN_ID,))
        assert "stats" in caps


def test_dashboard_access_allows_unlimited_role(tmp_path):
    path = _seed_access_db(tmp_path, expires_at=None)
    with dash_db.read_conn(path) as conn:
        caps = dash_access.resolve_capabilities(conn, MANAGER_ID, (ADMIN_ID,))
        assert "moderate_reg" in caps


def test_miniapp_reuses_the_exact_same_resolver_as_dashboard():
    """Mini App НЕ держит собственную копию резолва прав -- `miniapp/deps.py` импортирует
    `dashboard.access.resolve_capabilities` напрямую (см. докстринг miniapp/deps.py) -- значит
    любой тест на dashboard.access выше ОДНОВременно доказывает то же самое для Mini App, без
    поднятия FastAPI-приложения."""
    import miniapp.deps as miniapp_deps
    assert miniapp_deps.resolve_capabilities is dash_access.resolve_capabilities


# ── UI: render_roles_text / build_roles_keyboard показывают срок ────────────────────────────

def test_roles_text_shows_unlimited_by_default(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    text = _run(admin_roles.render_roles_text())
    assert "⏳ бессрочно" in text


def test_roles_text_shows_active_expiry_date(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2026-10-04"))
    text = _run(admin_roles.render_roles_text())
    assert "⏳ до 04.10.2026" in text


def test_roles_text_shows_expired_marker(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2020-01-01"))
    text = _run(admin_roles.render_roles_text())
    assert "⌛ истекла 01.01.2020" in text


def test_roles_keyboard_has_expiry_button_per_row(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID, expires_at="2026-10-04"))
    kb = _run(admin_roles.build_roles_keyboard(ADMIN_ID))
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"rexp:{MANAGER_ID}:reg_manager" in cbs


# ── Dispatch: экран «⏳ Срок действия роли» через настоящий роутер ──────────────────────────

def _grant(uid, role, *, by=ADMIN_ID, expires_at=None):
    _run(db.add_staff(uid, role, by, expires_at=expires_at))


def test_rexp_screen_shows_current_expiry(tmp_path):
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager", expires_at="2026-10-04")
    result, event = dispatch_callback(f"rexp:{MANAGER_ID}:reg_manager", ADMIN_ID)
    assert "до 04.10.2026" in event.message.text


def test_rexp_screen_hides_forum_button_without_forum_date(tmp_path):
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager")
    result, event = dispatch_callback(f"rexp:{MANAGER_ID}:reg_manager", ADMIN_ID)
    cbs = [b.callback_data for row in event.message.markup.inline_keyboard for b in row]
    assert not any(cb.startswith(f"rexp_go:{MANAGER_ID}:reg_manager:forum") for cb in cbs)


def test_rexp_go_forum_sets_expiry_from_forum_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("sos_active_days", "1"))
    _grant(MANAGER_ID, "reg_manager")
    expected = _run(forum_end_date_iso(None))
    assert expected == "2026-10-04"  # 03.10 однодневный форум -> истекает с 05.10 -> ISO 04.10 включительно

    result, event = dispatch_callback(f"rexp_go:{MANAGER_ID}:reg_manager:forum", ADMIN_ID)
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] == expected


def test_rexp_go_none_clears_expiry(tmp_path):
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager", expires_at="2026-10-04")
    dispatch_callback(f"rexp_go:{MANAGER_ID}:reg_manager:none", ADMIN_ID)
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] is None


def test_rexp_custom_date_step_sets_expiry(tmp_path):
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager")
    state = _fresh_state(ADMIN_ID)
    dispatch_callback(f"rexp_custom:{MANAGER_ID}:reg_manager", ADMIN_ID, state=state)
    dispatch_message("04.10.2026", ADMIN_ID, raw_state="RolesExpiryEdit:waiting_date", state=state)
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] == "2026-10-04"


def test_rexp_custom_date_step_rejects_bad_format(tmp_path):
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager")
    state = _fresh_state(ADMIN_ID)
    dispatch_callback(f"rexp_custom:{MANAGER_ID}:reg_manager", ADMIN_ID, state=state)
    result, event = dispatch_message(
        "не дата", ADMIN_ID, raw_state="RolesExpiryEdit:waiting_date", state=state,
    )
    assert "Не понял дату" in event.answers[0][0]
    staff = _run(db.list_staff())
    assert staff[0]["expires_at"] is None  # not touched by the rejected input


def test_rexp_requires_settings_capability(tmp_path):
    """CapabilityMiddleware: держатель права `checkin` (не `settings`) не может открыть экран
    срока роли (та же капа, что и весь остальной «👥 Роли и доступы»)."""
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager")
    volunteer = MANAGER_ID + 2
    _grant(volunteer, "volunteer")
    result, event = dispatch_callback(f"rexp:{MANAGER_ID}:reg_manager", volunteer)
    assert event.answers  # denial toast shown to a known staff member


# ── services/staff_expiry.py: разбор дат, парность строк ────────────────────────────────────

def test_parse_ddmmyyyy_valid():
    assert parse_ddmmyyyy("04.10.2026") == "2026-10-04"


def test_parse_ddmmyyyy_invalid_returns_none():
    assert parse_ddmmyyyy("31.02.2026") is None
    assert parse_ddmmyyyy("не дата") is None


def test_is_expiry_active_none_is_forever():
    assert is_expiry_active(None) is True


def test_is_expiry_active_boundary_today_is_active(monkeypatch):
    today = today_iso()
    assert is_expiry_active(today, today=today) is True


def test_is_expiry_active_past_is_inactive():
    assert is_expiry_active("2000-01-01") is False


# ── ROLES: волонтёр держит ровно checkin ────────────────────────────────────────────────────

def test_volunteer_role_default_caps_is_exactly_checkin():
    assert ROLES["volunteer"]["default_caps"] == ["checkin"]
