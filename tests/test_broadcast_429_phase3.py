"""Phase 3 (COMM-04) pure-helper tests for flood-safe send accounting."""
from handlers.admin import _retry_delay, _classify_outcome


def test_retry_delay():
    assert _retry_delay(0) == 1
    assert _retry_delay(5) == 6
    assert _retry_delay(30) == 31


def test_classify_delivered_first_try():
    assert _classify_outcome(True, None) == (1, 0)


def test_classify_retry_success_not_blocked():
    # 429 then retry succeeded -> delivered, NOT blocked (D-08)
    assert _classify_outcome(False, True) == (1, 0)


def test_classify_retry_failed_blocked():
    assert _classify_outcome(False, False) == (0, 1)


def test_classify_genuine_failure_blocked():
    assert _classify_outcome(False, "error") == (0, 1)
