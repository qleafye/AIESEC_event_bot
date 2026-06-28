"""Phase 3 (VERIF-01/02) pure-helper tests for the pre-selection allowlist."""
import services.allowlist as allowlist
from services.allowlist import _normalize, _parse_manual_ids, is_allowed


def test_normalize():
    assert _normalize(" @Ivan ") == "ivan"
    assert _normalize("IVAN") == "ivan"
    assert _normalize("@ivan") == "ivan"


def test_parse_manual_ids():
    assert _parse_manual_ids("123, 456 ,789") == {123, 456, 789}
    assert _parse_manual_ids("") == set()
    assert _parse_manual_ids("12,abc,34") == {12, 34}
    assert _parse_manual_ids(None) == set()


def test_is_allowed():
    allowlist._allowlist = {"ivan"}
    try:
        assert is_allowed("@Ivan") is True
        assert is_allowed("ivan") is True
        assert is_allowed(None) is False
        assert is_allowed("petr") is False
    finally:
        allowlist._allowlist = set()
