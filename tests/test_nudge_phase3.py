"""Phase 3 (SCHED-03) pure-helper tests for the dropout-nudge job."""
from datetime import datetime

from services.scheduler import _nudge_cutoff, _nudge_enabled, _int_or_default


def test_nudge_cutoff():
    assert _nudge_cutoff(datetime(2026, 7, 1, 12, 0, 0), 120) == "2026-07-01 10:00:00"


def test_nudge_enabled():
    assert _nudge_enabled(None) is True
    assert _nudge_enabled("on") is True
    assert _nudge_enabled("off") is False


def test_nudge_thresholds_default():
    assert _int_or_default(None, 120) == 120
    assert _int_or_default(None, 15) == 15
    assert _int_or_default("30", 120) == 30
