"""Phase 3 (COMM-02) pure-helper tests for the filtered-broadcast WHERE builder."""
import asyncio

from config import config
from database import db
from database.db import _build_filter_clause


def test_empty():
    assert _build_filter_clause([]) == ("", [])


def test_single_field():
    assert _build_filter_clause([{"field": "city", "value": "Москва"}]) == (
        " WHERE city = ?", ["Москва"]
    )


def test_two_fields_and():
    assert _build_filter_clause([
        {"field": "city", "value": "Москва"},
        {"field": "status", "value": "approved"},
    ]) == (" WHERE city = ? AND status = ?", ["Москва", "approved"])


def test_date_after():
    assert _build_filter_clause([
        {"field": "registration_date", "op": "after", "value": "2026-06-01"}
    ]) == (" WHERE registration_date >= ?", ["2026-06-01"])


def test_date_before():
    assert _build_filter_clause([
        {"field": "registration_date", "op": "before", "value": "2026-06-01"}
    ]) == (" WHERE registration_date < ?", ["2026-06-01"])


def test_injection_field_rejected():
    # non-whitelisted field is dropped, never interpolated
    assert _build_filter_clause([{"field": "DROP TABLE users", "value": "x"}]) == ("", [])


# ── ME-04: filtered broadcast must never fan out to ALL users on a degenerate filter ──

def _seed_users(n):
    async def go():
        await db.init_db()
        for i in range(1, n + 1):
            await db.add_user({"telegram_id": i, "full_name": f"U{i}", "city": "Москва",
                               "registration_date": "2026-07-01 10:00:00"})
    asyncio.run(go())


def test_count_and_list_filtered_degenerate_filter_returns_empty(tmp_path):
    """A non-empty filter whose fields are ALL non-whitelisted degenerates to where=''. It must
    return an EMPTY audience, not silently blast every user in the base."""
    config.DB_PATH = str(tmp_path / "me04.db")
    _seed_users(5)
    # Every field invalid → no surviving clause → must NOT match all 5 users.
    ids = asyncio.run(db.count_and_list_filtered([{"field": "DROP TABLE users", "value": "x"}]))
    assert ids == []


def test_count_and_list_filtered_valid_filter_still_works(tmp_path):
    config.DB_PATH = str(tmp_path / "me04b.db")
    _seed_users(3)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "city", "value": "Москва"}]))
    assert sorted(ids) == [1, 2, 3]
    ids_none = asyncio.run(db.count_and_list_filtered([{"field": "city", "value": "Питер"}]))
    assert ids_none == []
