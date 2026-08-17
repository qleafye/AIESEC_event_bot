"""Phase 09.3 (CITY-08): у города админки три состояния — `None` (модуль городов выключен),
`"*"` (`cities.ALL_CITIES`, «все города», модуль включён и фильтра нет) и реальный код города.

Контракт, который защищает этот файл:
    «"*" никогда не доходит до `normalize_city` — иначе "все города" тихо стали бы "только
    Москва". `None` по-прежнему значит ТОЛЬКО "модуль выключен" и ничего больше. Привязанный к
    городу менеджер не может встать в режим "все города" ни через хендлер, ни прямым вызовом
    `set_admin_city`. Оба пер-городных резолвера настроек в режиме "все города" отдают ОБЩЕЕ
    значение, а не значение города по умолчанию.»

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path; та же конвенция, что в
tests/test_manager_city_091.py / tests/test_city_scope_phase72.py.
"""
import asyncio

from config import config
from database import db
import cities


ADMIN_ID = 930201
MANAGER_ID = 930202


def _admin_ready(tmp_path, db_name="test_city_all_mode_093.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


# ── Task 1: city_scope / city_label / admin_selected_city / set_admin_city ─────────────────

def test_city_scope_all_cities_is_none():
    assert cities.city_scope(cities.ALL_CITIES) is None


def test_city_scope_none_is_still_none():
    assert cities.city_scope(None) is None


def test_city_scope_real_city_unchanged():
    assert cities.city_scope("spb") == ("spb", ())


def test_city_scope_all_cities_never_reaches_normalize_city(monkeypatch):
    calls = []
    real_normalize = cities.normalize_city

    def _spy(code):
        calls.append(code)
        return real_normalize(code)

    monkeypatch.setattr(cities, "normalize_city", _spy)
    assert cities.city_scope(cities.ALL_CITIES) is None
    assert calls == []


def test_city_label_all_cities_returns_constant(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        # An attempt to override the mode's label via the normal per-city key must be ignored.
        await db.set_setting(f"city_label__{cities.ALL_CITIES}", "Подделанное имя")
        return await cities.city_label(cities.ALL_CITIES)

    assert asyncio.run(go()) == cities.ALL_CITIES_LABEL == "🌍 Все города"


def test_set_admin_city_superadmin_all_cities_persists_marker(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        ok = await cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES)
        raw = await db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}")
        return ok, raw

    ok, raw = asyncio.run(go())
    assert ok is True
    assert raw == "*"


def test_admin_selected_city_returns_all_cities_marker_not_default(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES)
        return await cities.admin_selected_city(ADMIN_ID)

    assert asyncio.run(go()) == cities.ALL_CITIES


def test_admin_selected_city_module_off_returns_none_even_with_all_cities_stored(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        # Module is left at its default (off) — the "*" value from an earlier on-period must
        # not leak through once the module is disabled again.
        await db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES)
        return await cities.admin_selected_city(ADMIN_ID)

    assert asyncio.run(go()) is None


def test_set_admin_city_bound_manager_rejected_for_all_cities(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(MANAGER_ID, "spb")
        ok = await cities.set_admin_city(MANAGER_ID, cities.ALL_CITIES)
        raw = await db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{MANAGER_ID}")
        return ok, raw

    ok, raw = asyncio.run(go())
    assert ok is False
    assert raw is None


def test_set_admin_city_bound_manager_to_default_city_still_rejected_for_all_cities(tmp_path):
    """Latent-bug regression (found during planning): a manager bound to the DEFAULT city
    must still be refused ALL_CITIES — `normalize_city(ALL_CITIES) == normalize_city(bound)`
    would otherwise both resolve to `default_city_code()` and let the old comparison pass."""
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        default_code = cities.default_city_code()
        await db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(MANAGER_ID, default_code)
        ok = await cities.set_admin_city(MANAGER_ID, cities.ALL_CITIES)
        raw = await db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{MANAGER_ID}")
        return ok, raw

    ok, raw = asyncio.run(go())
    assert ok is False
    assert raw is None


def test_admin_selected_city_bound_manager_stays_own_city_even_if_all_cities_was_written(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID)
        # Simulate a stale/foreign "*" value saved before the binding existed.
        await db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{MANAGER_ID}", cities.ALL_CITIES)
        await db.set_staff_city(MANAGER_ID, "spb")
        return await cities.admin_selected_city(MANAGER_ID)

    assert asyncio.run(go()) == "spb"


def test_set_admin_city_unbound_manager_accepts_all_cities(tmp_path):
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID)
        ok = await cities.set_admin_city(MANAGER_ID, cities.ALL_CITIES)
        raw = await db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{MANAGER_ID}")
        return ok, raw

    ok, raw = asyncio.run(go())
    assert ok is True
    assert raw == "*"


def test_set_admin_city_unknown_code_still_rejected():
    async def go():
        return await cities.set_admin_city(ADMIN_ID, "нет_такого_кода")

    assert asyncio.run(go()) is False
