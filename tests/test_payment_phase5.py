"""Phase 5 (TRACK-05, D-16): optional third `track` field in payment_options.

D-16 compatibility guarantee: a bare 'label|price' line parses exactly as before Phase 5.
An optional third field accepts comma-separated track values.
"""
import handlers.payment as pay


# --- D-16: _parse_options backward compatibility (2-field lines unchanged) -----------------

def test_parse_two_field_line_unchanged():
    """D-16 compatibility guarantee: a bare 'label|price' line parses exactly as before
    Phase 5 — tracks is None, meaning offered to ALL tracks."""
    assert pay._parse_options("Полное участие|5000") == [("Полное участие", 5000, None)]


def test_parse_pipeless_line_unchanged():
    assert pay._parse_options("A") == [("A", 0, None)]


def test_parse_non_integer_price_falls_back_to_zero():
    assert pay._parse_options("A|xx") == [("A", 0, None)]


def test_parse_multiline_preserves_order_and_skips_blanks():
    raw = "A|100\n\nB|200\n"
    assert pay._parse_options(raw) == [("A", 100, None), ("B", 200, None)]


# --- D-16: optional third track field -------------------------------------------------------

def test_parse_single_track_field():
    result = pay._parse_options("Вечеринка|1500|party_overnight")
    assert result == [("Вечеринка", 1500, {"party_overnight"})]


def test_parse_multiple_comma_separated_tracks():
    result = pay._parse_options("Вечеринка|1500|party_overnight,party_noovernight")
    assert result[0][2] == {"party_overnight", "party_noovernight"}


def test_parse_strips_whitespace_around_third_field_and_commas():
    result = pay._parse_options("Вечеринка|1500| party_overnight , party_noovernight ")
    assert result[0][2] == {"party_overnight", "party_noovernight"}


def test_parse_empty_third_field_yields_none_not_empty_set():
    """D-16: 'label|price|' (blank third field) means "offered to all tracks", not
    "matches nobody" — None, never an empty set."""
    assert pay._parse_options("A|100|") == [("A", 100, None)]
