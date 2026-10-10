"""Строка истории баллов: число выводится один раз (раньше «+1616🪙» вместо «+16🪙»)."""
from handlers.user_actions import _format_coin_entry_line


def _row(delta):
    return {"timestamp": "2026-10-10 12:00:00", "delta": delta, "reason": "тест", "source": "manual"}


def test_positive_delta_printed_once():
    assert _format_coin_entry_line(_row(16), "m", "t") == "10.10 +16🪙 — тест"


def test_negative_delta_printed_once():
    assert _format_coin_entry_line(_row(-21), "m", "t") == "10.10 -21🪙 — тест"


def test_zero_delta():
    assert _format_coin_entry_line(_row(0), "m", "t") == "10.10 +0🪙 — тест"
