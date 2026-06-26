"""Phase 2 pure-helper tests: approval status decision + menu gating."""
from handlers.registration import _decide_status
from handlers.user_actions import _gate_decision


# ── _decide_status (D-01..D-03) ──────────────────────────────────────────────

def test_decide_status_full_manual_is_pending():
    assert _decide_status("full", "manual", "auto") == "pending"


def test_decide_status_short_default_is_approved():
    assert _decide_status("short", "manual", "auto") == "approved"


def test_decide_status_short_manual_is_pending():
    assert _decide_status("short", "manual", "manual") == "pending"


def test_decide_status_full_auto_is_approved():
    assert _decide_status("full", "auto", "auto") == "approved"


# ── _gate_decision (D-05) ────────────────────────────────────────────────────

def test_gate_approved_allowed():
    assert _gate_decision("approved") == (True, None)


def test_gate_pending_denied():
    assert _gate_decision("pending") == (False, "pending")


def test_gate_rejected_denied():
    assert _gate_decision("rejected") == (False, "rejected")


def test_gate_legacy_none_allowed():
    assert _gate_decision(None) == (True, None)
