"""Phase 07.2 Plan 04 (per-city admin panels, CITY-02) tests: «Город мероприятия» as a
broadcast-segment filter.

Two halves, mirroring the two failure modes this plan exists to prevent:

1. `database.db` — `event_city` must be a legal filter column, the DEFAULT city must reach
   delegates whose `event_city IS NULL` (they are the entire pre-cities backlog), and an
   empty value must degenerate into the existing ME-04 fail-safe (empty audience) instead of
   a base-wide blast.
2. `handlers.admin` — the field must be registered in BOTH `_PICKER_FIELDS` and
   `db._FILTER_COLUMNS`. A field present in only one of the two is SILENTLY dropped
   (Phase 5 D-19): the manager sees the filter on screen, the SQL never receives it, and the
   broadcast goes to the wrong segment with nothing on screen to hint at it. Hence the
   two-way registration test.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_export_stats_phase72.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import json

from config import config
from database import db
from database.db import _build_filter_clause


# ── Task 1: event_city in the filter whitelist + NULL-collapse branch ───────────────────

def test_event_city_is_whitelisted():
    assert "event_city" in db._FILTER_COLUMNS


def test_event_city_plain_equality_when_exclude_empty():
    assert _build_filter_clause([
        {"field": "event_city", "value": "spb", "exclude": []}
    ]) == (" WHERE event_city = ?", ["spb"])


def test_event_city_equality_when_exclude_key_absent():
    """A filter dict without the `exclude` key at all still works (plain equality) — the key
    is optional, never required."""
    assert _build_filter_clause([
        {"field": "event_city", "value": "spb"}
    ]) == (" WHERE event_city = ?", ["spb"])


def test_event_city_default_city_collapses_null():
    """The default city is described by EXCLUSION so it also catches event_city IS NULL —
    every application registered before the cities module existed."""
    assert _build_filter_clause([
        {"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]}
    ]) == (" WHERE (event_city IS NULL OR event_city NOT IN (?, ?))", ["spb", "tyumen"])


def test_event_city_combined_with_other_field_keeps_and_and_bind_order():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]},
    ])
    assert where == (
        " WHERE status = ? AND (event_city IS NULL OR event_city NOT IN (?, ?))"
    )
    assert params == ["approved", "spb", "tyumen"]

    where2, params2 = _build_filter_clause([
        {"field": "event_city", "value": "spb", "exclude": []},
        {"field": "status", "value": "approved"},
    ])
    assert where2 == " WHERE event_city = ? AND status = ?"
    assert params2 == ["spb", "approved"]


def test_event_city_empty_value_is_dropped():
    """Never emit a condition without a value — an empty value must drop the filter, after
    which count_and_list_filtered's ME-04 fail-safe returns an empty audience."""
    assert _build_filter_clause([{"field": "event_city", "value": ""}]) == ("", [])
    assert _build_filter_clause([{"field": "event_city", "value": None}]) == ("", [])


def test_event_city_filter_survives_json_round_trip():
    """A scheduled broadcast stores its filter spec as JSON and rebuilds the audience after a
    restart — the `exclude` key must survive dumps/loads and produce the SAME condition."""
    filters = [{"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before


# ── Task 1 end-to-end: the audience query on a seeded base ─────────────────────────────

def _seed_broadcast_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_broadcast72.db")

    async def go():
        await db.init_db()
        for tid, city in ((1, None), (2, "msk"), (3, "spb"), (4, "spb"), (5, "tyumen")):
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
                "event_city": city,
            })

    asyncio.run(go())


def test_count_and_list_filtered_default_city_includes_null_rows(tmp_path):
    _seed_broadcast_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([
        {"field": "event_city", "value": "msk", "exclude": ["spb", "tyumen"]}
    ]))
    assert set(ids) == {1, 2}


def test_count_and_list_filtered_non_default_city_excludes_null_rows(tmp_path):
    _seed_broadcast_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([
        {"field": "event_city", "value": "spb", "exclude": []}
    ]))
    assert set(ids) == {3, 4}


def test_count_and_list_filtered_empty_city_value_returns_empty_audience(tmp_path):
    """Degenerate city filter must NEVER fan out to the whole base (ME-04 fail-safe)."""
    _seed_broadcast_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "event_city", "value": ""}]))
    assert ids == []
