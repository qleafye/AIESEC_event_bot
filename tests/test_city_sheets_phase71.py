"""Phase 07.1 Plan 02 (per-city sheet tab routing, CITY-02) tests.

pytest-asyncio is unavailable in this env — each async test drives the DB/registration
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file, same convention as
tests/test_cities_phase71.py / tests/test_sheets_phase5.py.
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum_city_sheets.db")


# ── Task 1: _sheet_kind / city_row_tab / city_incomplete_tab ────────────────────────────────

def test_sheet_kind_table():
    assert reg._sheet_kind(None) == "main"
    assert reg._sheet_kind("full") == "main"
    assert reg._sheet_kind("short") == "short"
    assert reg._sheet_kind("party_overnight") == "party"
    assert reg._sheet_kind("party_noovernight") == "party"


def test_city_row_tab_none_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg.city_row_tab("spb", None)

    assert asyncio.run(go()) is None


def test_city_row_tab_moscow_regression_all_tracks(tmp_path):
    """The Москва regression that matters most this phase: byte-identical to today (None ==
    legacy appender) across every track, once the module is on."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return (
            await reg.city_row_tab(None, None),
            await reg.city_row_tab("msk", None),
            await reg.city_row_tab("msk", "short"),
            await reg.city_row_tab("msk", "party_overnight"),
            await reg.city_row_tab("atlantis", None),  # unknown code -> Moscow fallback
        )

    results = asyncio.run(go())
    assert results == (None, None, None, None, None)


def test_city_row_tab_non_default_city_main_short_party(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return (
            await reg.city_row_tab("spb", None),
            await reg.city_row_tab("spb", "short"),
            await reg.city_row_tab("spb", "party_overnight"),
            await reg.city_row_tab("spb", "party_noovernight"),
            await reg.city_row_tab("tyumen", None),
            await reg.city_row_tab("tyumen", "short"),
        )

    results = asyncio.run(go())
    assert results == ("СПб", "СПб Акция", "СПб Party", "СПб Party", "Тюмень", "Тюмень Акция")


def test_city_row_tab_respects_tab_base_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_tab__spb", "Питер")
        return await reg.city_row_tab("spb", "short")

    assert asyncio.run(go()) == "Питер Акция"


def test_city_incomplete_tab_default_and_moscow(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        before_toggle = await reg.city_incomplete_tab(None)
        await db.set_setting("event_city_enabled", "on")
        return (
            before_toggle,
            await reg.city_incomplete_tab(None),
            await reg.city_incomplete_tab("msk"),
            await reg.city_incomplete_tab("spb"),
        )

    results = asyncio.run(go())
    assert results == ("Незавершённые", "Незавершённые", "Незавершённые", "СПб Незавершённые")
