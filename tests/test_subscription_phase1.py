"""Phase 1 subscription tests: membership mapping, fail-open check, segment authorization."""
import asyncio
from pathlib import Path

from handlers.registration import _membership_status_to_bool, is_subscribed

ADMIN_PY = Path(__file__).resolve().parent.parent / "handlers" / "admin.py"


# ── membership status mapping ────────────────────────────────────────────────

def test_member_statuses_true():
    for s in ("creator", "administrator", "member"):
        assert _membership_status_to_bool(s) is True


def test_non_member_statuses_false():
    for s in ("left", "kicked", "restricted"):
        assert _membership_status_to_bool(s) is False


# ── is_subscribed fails open (returns None, never raises) ─────────────────────

class _RaisingBot:
    async def get_chat_member(self, channel, user_id):
        raise RuntimeError("bot is not an admin in the channel")


def test_is_subscribed_fails_open_on_error():
    result = asyncio.run(is_subscribed(_RaisingBot(), "@somechannel", 123))
    assert result is None  # no exception propagated, nothing flagged


class _MemberBot:
    async def get_chat_member(self, channel, user_id):
        class M:
            status = "member"
        return M()


def test_is_subscribed_true_for_member():
    assert asyncio.run(is_subscribed(_MemberBot(), "@c", 1)) is True


# ── broadcast segments exist and are admin-gated ─────────────────────────────

def test_segment_handlers_present_and_admin_gated():
    src = ADMIN_PY.read_text(encoding="utf-8")
    assert "broadcast_unsubscribed" in src
    assert "broadcast_incomplete" in src
    assert "get_non_subscriber_ids" in src
    assert "get_incomplete_user_ids" in src
    # Each segment callback handler must enforce admin authorization.
    for marker in ("process_broadcast_unsubscribed", "process_broadcast_incomplete"):
        idx = src.index(f"async def {marker}")
        body = src[idx: idx + 400]
        assert "config.ADMIN_IDS" in body, f"{marker} missing admin check"
