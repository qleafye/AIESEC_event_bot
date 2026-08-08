"""Phase 07.2 Plan 01 (per-city admin panels, CITY-02) tests: the city scope layer —
`cities.city_scope` / `admin_selected_city` / `set_admin_city` and
`database.db._city_clause` + the `city_scope=` kwarg on the moderation/export queries.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_cities_phase71.py / tests/test_city_admin_phase71.py.
"""
import asyncio

from config import config
from database import db
import cities


ADMIN_ID = 910101


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_scope72.db")


# ── Task 1: cities.city_scope / admin_selected_city / set_admin_city ────────────

def test_city_scope_none_is_no_scope():
    assert cities.city_scope(None) is None


def test_city_scope_non_default_city_is_equality():
    assert cities.city_scope("spb") == ("spb", ())


def test_city_scope_default_city_excludes_the_others():
    assert cities.city_scope("msk") == ("msk", ("spb", "tyumen"))


def test_city_scope_unknown_code_collapses_to_default_via_normalize():
    assert cities.city_scope("garbage") == cities.city_scope("msk")


def test_admin_selected_city_module_off_is_always_none(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "off")
        await db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb")
        assert await cities.admin_selected_city(ADMIN_ID) is None

    asyncio.run(go())


def test_admin_selected_city_module_on_empty_setting_defaults_to_msk(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        assert await cities.admin_selected_city(ADMIN_ID) == "msk"

    asyncio.run(go())


def test_admin_selected_city_module_on_reads_stored_choice(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb")
        assert await cities.admin_selected_city(ADMIN_ID) == "spb"

    asyncio.run(go())


def test_set_admin_city_known_code_writes_and_returns_true(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        ok = await cities.set_admin_city(ADMIN_ID, "spb")
        assert ok is True
        assert await db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}") == "spb"

    asyncio.run(go())


def test_set_admin_city_unknown_code_rejected_and_writes_nothing(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        ok = await cities.set_admin_city(ADMIN_ID, "'; DROP")
        assert ok is False
        assert await db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}") is None

    asyncio.run(go())
