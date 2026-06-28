"""Phase 3 (SCHED-01) pure-helper tests for the scheduler service."""
from datetime import datetime

from services.scheduler import _int_or_default, _parse_schedule_dt, _fmt_dt


def test_int_or_default():
    assert _int_or_default("15", 60) == 15
    assert _int_or_default(None, 60) == 60
    assert _int_or_default("abc", 60) == 60
    assert _int_or_default("0", 60) == 60
    assert _int_or_default("-3", 60) == 60


def test_parse_schedule_dt_valid():
    assert _parse_schedule_dt("01.07.2026 14:30") == datetime(2026, 7, 1, 14, 30)


def test_parse_schedule_dt_invalid():
    assert _parse_schedule_dt("garbage") is None
    assert _parse_schedule_dt(None) is None
    assert _parse_schedule_dt("2026-07-01") is None


def test_fmt_dt():
    assert _fmt_dt(datetime(2026, 7, 1, 14, 30, 0)) == "2026-07-01 14:30:00"
