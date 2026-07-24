"""Phase 1 subscription tests: membership mapping, fail-open check, segment authorization."""
import asyncio
from pathlib import Path

from config import config
from database import db
from handlers import registration as reg
from handlers.registration import _membership_status_to_bool, is_subscribed, _normalize_channel_ref

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


# ── HG-02: contact_tg link → get_chat_member ref normalization ───────────────

def test_normalize_channel_ref_urls_to_username():
    for raw in ("https://t.me/mychan", "http://t.me/mychan", "t.me/mychan",
                "telegram.me/mychan", "@mychan", "mychan", "  https://t.me/mychan  ",
                "https://t.me/mychan?start=x"):
        assert _normalize_channel_ref(raw) == "@mychan", raw


def test_normalize_channel_ref_numeric_chat_id():
    assert _normalize_channel_ref("-1001234567890") == -1001234567890


def test_normalize_channel_ref_unresolvable_returns_none():
    # Private invite links / joinchat / c-paths / empty cannot resolve to a public @username.
    for raw in (None, "", "   ", "https://t.me/+AbCdEf", "t.me/joinchat/HASH",
                "https://t.me/c/123/45", "@"):
        assert _normalize_channel_ref(raw) is None, raw


# ── HG-01: first-touch registrant persists the subscription flag at finalize ─

class _FakeFromUser:
    def __init__(self, telegram_id):
        self.id = telegram_id
        self.username = "tester"


class _FakeMessage:
    def __init__(self, telegram_id):
        self.from_user = _FakeFromUser(telegram_id)

    async def answer(self, *args, **kwargs):
        pass


class _FakeState:
    def __init__(self, data):
        self._data = data

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        pass


def test_finalize_persists_subscription_for_new_user(tmp_path, monkeypatch):
    """HG-01 regression: cmd_start's set_user_subscribed runs before a brand-new user's row
    exists (UPDATE 0 rows). finalize_registration must persist the flag AFTER add_user so a
    first-touch registrant lands correctly in/out of the «не подписаны» segment."""
    config.DB_PATH = str(tmp_path / "sub.db")
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    async def fake_is_subscribed(bot, channel, user_id):
        # Assert HG-02 normalization ran: a t.me link must arrive here as @username.
        assert channel == "@mychan", channel
        return True

    monkeypatch.setattr(reg, "is_subscribed", fake_is_subscribed)

    uid = 987654
    message = _FakeMessage(uid)
    state = _FakeState({"full_name": "New User", "participant_type": "full"})

    async def go():
        await db.init_db()
        await db.set_setting("contact_tg", "https://t.me/mychan")  # stored as a display link
        await db.set_setting("full_approval", "manual")  # stays pending → no bot sends needed
        assert await db.get_user(uid) is None  # brand-new, no row yet
        await reg.finalize_registration(message, state, bot=None)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        return await db.get_user(uid)

    row = asyncio.run(go())
    assert row is not None
    assert row["subscribed"] == 1  # flag persisted despite row not existing at cmd_start time


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
