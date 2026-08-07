"""Phase 07.1 Plan 01 (multi-city registration foundation, CITY-01) tests.

pytest-asyncio is unavailable in this env — each async test drives the DB/registration
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file, same convention as
tests/test_registration_phase5.py / tests/test_db_phase5.py.
"""
import asyncio

from config import config
from database import db
import cities


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum_cities.db")


# ── Task 1: cities.py registry ──────────────────────────────────────────────────

def test_parse_cities_default_gives_three_entries_in_order():
    parsed = cities.parse_cities(config.EVENT_CITIES)
    assert [c["code"] for c in parsed] == ["msk", "spb", "tyumen"]


def test_parse_cities_default_tab_bases():
    parsed = {c["code"]: c for c in cities.parse_cities(config.EVENT_CITIES)}
    assert parsed["msk"]["tab_base"] == ""
    assert parsed["spb"]["tab_base"] == "СПб"
    assert parsed["tyumen"]["tab_base"] == "Тюмень"


def test_parse_cities_default_labels():
    parsed = {c["code"]: c for c in cities.parse_cities(config.EVENT_CITIES)}
    assert parsed["msk"]["label"] == "Москва, 30-31 октября"
    assert parsed["spb"]["label"] == "Санкт-Петербург, 3 октября"
    assert parsed["tyumen"]["label"] == "Тюмень, 3 октября"


def test_parse_cities_empty_string():
    assert cities.parse_cities("") == []


def test_parse_cities_garbage_does_not_raise():
    assert cities.parse_cities("мусор;;|||") == []


def test_parse_cities_skips_empty_label():
    assert cities.parse_cities("spb||СПб") == []


def test_parse_cities_skips_non_ascii_code():
    assert cities.parse_cities("спб|Санкт-Петербург|СПб") == []


def test_parse_cities_duplicate_code_first_wins():
    parsed = cities.parse_cities("spb|First, 1 октября|СПб;spb|Second, 2 октября|Питер")
    assert len(parsed) == 1
    assert parsed[0]["label"] == "First, 1 октября"
    assert parsed[0]["tab_base"] == "СПб"


def test_normalize_city_unknown_and_empty_resolve_to_msk():
    assert cities.normalize_city(None) == "msk"
    assert cities.normalize_city("") == "msk"
    assert cities.normalize_city("atlantis") == "msk"


def test_normalize_city_known_code_passes_through():
    assert cities.normalize_city("spb") == "spb"


def test_is_city_enabled_default_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await cities.is_city_enabled("spb") is True

    asyncio.run(go())


def test_is_city_enabled_can_be_turned_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("city_enabled__spb", "off")
        assert await cities.is_city_enabled("spb") is False

    asyncio.run(go())


def test_city_label_default_from_registry(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await cities.city_label("spb") == "Санкт-Петербург, 3 октября"

    asyncio.run(go())


def test_city_label_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("city_label__spb", "СПб, 4 октября")
        assert await cities.city_label("spb") == "СПб, 4 октября"

    asyncio.run(go())


def test_city_tab_base_default_from_registry(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await cities.city_tab_base("msk") == ""
        assert await cities.city_tab_base("spb") == "СПб"

    asyncio.run(go())


def test_city_tab_base_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("city_tab__spb", "Питер")
        assert await cities.city_tab_base("spb") == "Питер"

    asyncio.run(go())


# ── Task 2: DB migrations — users.event_city / reg_started.event_city ──────────

def test_init_db_twice_is_idempotent(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.init_db()  # must not raise

    asyncio.run(go())


def test_add_user_with_event_city_roundtrips(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 1,
            "registration_date": "2026-08-08",
            "event_city": "spb",
        })
        user = await db.get_user(1)
        assert user["event_city"] == "spb"

    asyncio.run(go())


def test_add_user_without_event_city_stores_null(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 2,
            "registration_date": "2026-08-08",
        })
        user = await db.get_user(2)
        assert user["event_city"] is None
        assert cities.normalize_city(user["event_city"]) == "msk"

    asyncio.run(go())


def test_mark_reg_started_coalesce_does_not_wipe_city(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.mark_reg_started(10, "u", "full", "spb")
        await db.mark_reg_started(10, "u", None, None)
        assert await db.get_reg_started_city(10) == "spb"

    asyncio.run(go())


def test_pre_migration_row_reads_event_city_as_none(tmp_path):
    """A users row inserted via the bare CREATE TABLE schema (before _ensure_column added
    event_city) must read back as None after the column is added — proving the ADD COLUMN
    migration doesn't require the row to already have the column."""
    _use_tmp_db(tmp_path)

    async def go():
        import aiosqlite
        # Simulate a pre-migration DB: create the base table without event_city, insert a
        # row, THEN run init_db() (which additively adds the column).
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute('''
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    registration_date TEXT
                )
            ''')
            await conn.execute(
                "INSERT INTO users (telegram_id, username, full_name, registration_date) "
                "VALUES (?, ?, ?, ?)",
                (99, "legacy", "Legacy User", "2020-01-01"),
            )
            await conn.commit()

        await db.init_db()
        user = await db.get_user(99)
        assert user["event_city"] is None

    asyncio.run(go())


def test_no_backfill_null_stays_null_across_repeated_init(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.add_user({"telegram_id": 3, "registration_date": "2026-08-08"})
        await db.init_db()  # re-run migrations again
        user = await db.get_user(3)
        assert user["event_city"] is None
        assert cities.normalize_city(user["event_city"]) == "msk"

    asyncio.run(go())
