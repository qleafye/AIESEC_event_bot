"""Phase 2 (APP-08) pure-helper tests for the pending reminder config."""
from services.reminders import _reminder_enabled, _reminder_interval, DEFAULT_INTERVAL


def test_enabled_default_on():
    assert _reminder_enabled(None) is True
    assert _reminder_enabled("on") is True


def test_enabled_off():
    assert _reminder_enabled("off") is False


def test_interval_valid():
    assert _reminder_interval("60") == 60


def test_interval_default_on_none():
    assert _reminder_interval(None) == DEFAULT_INTERVAL


def test_interval_default_on_garbage():
    assert _reminder_interval("abc") == DEFAULT_INTERVAL


def test_interval_default_on_nonpositive():
    assert _reminder_interval("0") == DEFAULT_INTERVAL
    assert _reminder_interval("-5") == DEFAULT_INTERVAL
