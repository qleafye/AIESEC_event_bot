"""CSV/Excel formula-injection neutralizer tests (CR-6).

Pure unit test — no bot/DB/async fixtures. `_csv_safe` is a plain function.
"""
from database.db import _csv_safe


def test_csv_safe_neutralizes_equals_prefix():
    assert _csv_safe("=SUM(A1)") == "'=SUM(A1)"


def test_csv_safe_neutralizes_plus_prefix():
    assert _csv_safe("+1") == "'+1"


def test_csv_safe_neutralizes_minus_prefix():
    assert _csv_safe("-1") == "'-1"


def test_csv_safe_neutralizes_at_prefix():
    assert _csv_safe("@x") == "'@x"


def test_csv_safe_neutralizes_leading_tab():
    assert _csv_safe("\tx") == "'\tx"


def test_csv_safe_neutralizes_leading_cr():
    assert _csv_safe("\rx") == "'\rx"


def test_csv_safe_leaves_normal_string_unchanged():
    assert _csv_safe("normal") == "normal"


def test_csv_safe_leaves_empty_string_unchanged():
    assert _csv_safe("") == ""


def test_csv_safe_passes_through_int_unchanged():
    assert _csv_safe(123) == 123


def test_csv_safe_passes_through_none_unchanged():
    assert _csv_safe(None) is None
