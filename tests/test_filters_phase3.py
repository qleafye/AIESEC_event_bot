"""Phase 3 (COMM-02) pure-helper tests for the filtered-broadcast WHERE builder."""
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
