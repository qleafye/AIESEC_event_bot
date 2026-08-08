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


# ── Task 2: database.db._city_clause + city_scope= on the moderation queries ────

def test_city_clause_no_scope_is_empty():
    assert db._city_clause(None) == ("", [])


def test_city_clause_equality_for_non_default_city():
    assert db._city_clause(("spb", ())) == ("event_city = ?", ["spb"])


def test_city_clause_exclusion_for_default_city():
    assert db._city_clause(("msk", ("spb", "tyumen"))) == (
        "(event_city IS NULL OR event_city NOT IN (?, ?))",
        ["spb", "tyumen"],
    )


def _seed_city(telegram_id, event_city, status="pending", payment_status=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": "2026-01-01 09:00:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))
    if payment_status is not None:
        asyncio.run(db.update_payment_status(telegram_id, payment_status))


def _seed_five_cities(tmp_path, status="pending"):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed_city(1, None, status=status)
    _seed_city(2, "msk", status=status)
    _seed_city(3, "spb", status=status)
    _seed_city(4, "tyumen", status=status)
    _seed_city(5, "garbage", status=status)


def test_get_pending_count_no_scope_matches_today(tmp_path):
    _seed_five_cities(tmp_path)
    assert asyncio.run(db.get_pending_count()) == 5


def test_get_pending_count_city_scoped(tmp_path):
    _seed_five_cities(tmp_path)
    assert asyncio.run(db.get_pending_count(city_scope=cities.city_scope("spb"))) == 1
    # default city collapses NULL + msk + garbage -> 3
    assert asyncio.run(db.get_pending_count(city_scope=cities.city_scope("msk"))) == 3


def test_get_pending_users_city_scoped_returns_only_that_city(tmp_path):
    _seed_five_cities(tmp_path)
    rows = asyncio.run(db.get_pending_users(limit=10, city_scope=cities.city_scope("spb")))
    assert [r["telegram_id"] for r in rows] == [3]


def test_approve_all_pending_blast_radius_does_not_touch_other_cities(tmp_path):
    _seed_five_cities(tmp_path)
    flipped = asyncio.run(db.approve_all_pending(city_scope=cities.city_scope("spb")))
    assert flipped == [3]

    async def check():
        for tid in (1, 2, 4, 5):
            user = await db.get_user(tid)
            assert user["status"] == "pending"
        user3 = await db.get_user(3)
        assert user3["status"] == "approved"

    asyncio.run(check())


def test_approve_all_pending_no_scope_flips_everyone(tmp_path):
    _seed_five_cities(tmp_path)
    flipped = asyncio.run(db.approve_all_pending())
    assert sorted(flipped) == [1, 2, 3, 4, 5]
    assert asyncio.run(db.get_pending_count()) == 0


def test_receipt_pending_queue_city_scoped(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed_city(1, None, status="approved", payment_status="receipt_sent")
    _seed_city(2, "spb", status="approved", payment_status="receipt_sent")
    _seed_city(3, "tyumen", status="approved", payment_status="receipt_sent")

    assert asyncio.run(db.get_receipt_pending_count()) == 3
    assert asyncio.run(
        db.get_receipt_pending_count(city_scope=cities.city_scope("spb"))
    ) == 1
    assert asyncio.run(
        db.get_receipt_pending_count(city_scope=cities.city_scope("msk"))
    ) == 1  # msk-default collapse catches the NULL row (no explicit 'msk' row here)

    rows = asyncio.run(
        db.get_receipt_pending_users(limit=10, city_scope=cities.city_scope("tyumen"))
    )
    assert [r["telegram_id"] for r in rows] == [3]


# ── Task 3: get_city_counts() + city-scoped export_users_csv ────────────────────

def test_get_city_counts_returns_raw_event_city_values_including_null_and_garbage(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed_city(1, None, status="pending")
    _seed_city(2, "msk", status="approved")
    _seed_city(3, "spb", status="pending")
    _seed_city(4, "garbage", status=None)

    rows = asyncio.run(db.get_city_counts())
    by_city = {row[0]: row for row in rows}

    assert set(by_city.keys()) == {None, "msk", "spb", "garbage"}
    # (event_city, total, pending, approved)
    assert by_city[None] == (None, 1, 1, 0)
    assert by_city["msk"] == ("msk", 1, 0, 1)
    assert by_city["spb"] == ("spb", 1, 1, 0)
    # status IS NULL counts toward total but neither pending nor approved
    assert by_city["garbage"] == ("garbage", 1, 0, 0)


def test_export_users_csv_no_scope_matches_today(tmp_path):
    _seed_five_cities(tmp_path)
    headers, rows = asyncio.run(db.export_users_csv())
    assert len(rows) == 5
    assert len(headers) > 0


def test_export_users_csv_scoped_headers_identical_to_unscoped(tmp_path):
    _seed_five_cities(tmp_path)
    headers_unscoped, _ = asyncio.run(db.export_users_csv())
    headers_scoped, _ = asyncio.run(db.export_users_csv(city_scope=cities.city_scope("spb")))
    assert headers_unscoped == headers_scoped


def test_export_users_csv_spb_scope_only_spb_rows(tmp_path):
    _seed_five_cities(tmp_path)
    _, rows = asyncio.run(db.export_users_csv(city_scope=cities.city_scope("spb")))
    tid_idx = None

    async def get_idx():
        nonlocal tid_idx
        headers, _ = await db.export_users_csv()
        tid_idx = headers.index("ID Telegram")

    asyncio.run(get_idx())
    assert [r[tid_idx] for r in rows] == [3]


def test_export_users_csv_msk_scope_includes_null_event_city_row(tmp_path):
    _seed_five_cities(tmp_path)
    headers, rows = asyncio.run(db.export_users_csv(city_scope=cities.city_scope("msk")))
    tid_idx = headers.index("ID Telegram")
    assert sorted(r[tid_idx] for r in rows) == [1, 2, 5]  # NULL, msk, garbage
