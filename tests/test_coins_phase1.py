"""Phase 1 coins tests: /coins arg parsing, leaderboard rendering, admin-only guard."""
from pathlib import Path

from handlers.admin import _parse_coins_amount
from handlers.user_actions import render_leaderboard

ADMIN_PY = Path(__file__).resolve().parent.parent / "handlers" / "admin.py"


# ── /coins amount parsing ────────────────────────────────────────────────────

def test_parse_plus():
    assert _parse_coins_amount("+10") == 10


def test_parse_minus():
    assert _parse_coins_amount("-3") == -3


def test_parse_unsigned():
    assert _parse_coins_amount("10") == 10


def test_parse_non_numeric():
    assert _parse_coins_amount("abc") is None


def test_parse_empty():
    assert _parse_coins_amount("") is None


# ── /coins is admin-only (structural — authorization not bypassable) ──────────

def test_coins_handler_is_admin_guarded():
    src = ADMIN_PY.read_text(encoding="utf-8")
    # The /coins handler decorator must include the is_admin filter.
    assert 'Command("coins")' in src
    line = next(l for l in src.splitlines() if 'Command("coins")' in l)
    assert "is_admin" in line


# ── leaderboard rendering ────────────────────────────────────────────────────

def test_render_leaderboard_lists_names_ranked():
    rows = [
        {"user_id": 1, "balance": 90, "full_name": "Alice"},
        {"user_id": 2, "balance": 50, "full_name": "Bob"},
    ]
    out = render_leaderboard(rows, requester_id=2, requester_rank=2, requester_balance=50)
    assert "Alice" in out and "Bob" in out
    assert "1. Alice" in out          # ranked, top first
    assert "Твоё место" in out and "2" in out  # requester rank line


def test_render_leaderboard_escapes_html():
    rows = [{"user_id": 1, "balance": 10, "full_name": "<b>x</b>"}]
    out = render_leaderboard(rows, requester_id=1, requester_rank=1, requester_balance=10)
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_render_leaderboard_empty():
    out = render_leaderboard([], requester_id=1, requester_rank=None, requester_balance=0)
    assert "Пока" in out
    assert "—" in out  # no rank shown for a user with no coins
