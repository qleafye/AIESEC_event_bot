"""BLOCK 7 (LOW) regressions.

- Main-tab formula-injection parity: active_sheet_row must neutralize a crafted cell via
  _csv_safe, matching the party path.
- Negative-amount guard: _parse_options must clamp a negative price to 0.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from handlers import registration as reg
# Phase 13 REFAC (13-03): _validate_date_range moved to handlers/reg_flow.py alongside
# process_date_input, its sole caller.
from handlers.reg_flow import _validate_date_range
from handlers.payment import _parse_options


def test_main_tab_active_sheet_row_neutralizes_formula(tmp_path):
    config.DB_PATH = str(tmp_path / "csv_main.db")

    async def go():
        await db.init_db()
        data = {
            "telegram_id": 1,
            "full_name": "=HYPERLINK(\"http://evil\",\"click\")",
            "registration_date": "2026-07-01 10:00:00",
        }
        return await reg.active_sheet_row(data)

    row = asyncio.run(go())
    # The crafted ФИО must be stored as text (leading apostrophe), never left to evaluate.
    assert any(isinstance(c, str) and c.startswith("'=HYPERLINK") for c in row), row
    assert not any(isinstance(c, str) and c.startswith("=HYPERLINK") for c in row), row


def test_parse_options_clamps_negative_price():
    opts = _parse_options("Скидка|-500")
    assert opts == [("Скидка", 0, None)]  # negative clamped to 0


def test_parse_options_keeps_valid_price():
    assert _parse_options("Участие|3000") == [("Участие", 3000, None)]


# ── receipt upload hardening: size cap + rate limit ──────────────────────────

def test_receipt_too_large_boundary():
    from handlers.payment import _receipt_too_large, _RECEIPT_MAX_BYTES
    assert _receipt_too_large(None) is False
    assert _receipt_too_large(0) is False
    assert _receipt_too_large(_RECEIPT_MAX_BYTES) is False       # exactly at cap: allowed
    assert _receipt_too_large(_RECEIPT_MAX_BYTES + 1) is True    # over cap: rejected


def test_receipt_rate_limit_blocks_rapid_second_upload():
    from handlers import payment
    payment._last_receipt_upload.clear()
    uid = 12345
    assert payment._receipt_rate_limited(uid) is False  # first upload accepted
    assert payment._receipt_rate_limited(uid) is True   # immediate second blocked
    assert payment._receipt_rate_limited(99999) is False  # a different user is independent


# ── date-range validators ────────────────────────────────────────────────────

def test_validate_birth_date_range():
    now = datetime.now()
    assert _validate_date_range("birth_date", now + timedelta(days=1)) is not None  # future DOB
    assert _validate_date_range("birth_date", datetime(now.year - 5, 1, 1)) is not None  # too young
    assert _validate_date_range("birth_date", datetime(1800, 1, 1)) is not None  # absurd year
    assert _validate_date_range("birth_date", datetime(2000, 5, 15)) is None  # plausible → ok


def test_validate_arrival_date_range():
    now = datetime.now()
    assert _validate_date_range("arrival_date", now - timedelta(days=2)) is not None  # past
    assert _validate_date_range("arrival_date", datetime(now.year + 5, 1, 1)) is not None  # too far
    assert _validate_date_range("arrival_date", now + timedelta(days=30)) is None  # soon → ok


def test_validate_date_range_unknown_step_is_permissive():
    assert _validate_date_range("some_other_date", datetime(1990, 1, 1)) is None


# ── consent re-verify at finalize is non-blocking ────────────────────────────

class _FMsg:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid, "username": "u"})()

    async def answer(self, *a, **k):
        pass


class _FState:
    def __init__(self, d):
        self._d = d

    async def get_data(self):
        return dict(self._d)

    async def clear(self):
        pass


def test_consent_missing_does_not_block_finalize(tmp_path, monkeypatch, caplog):
    """A missing consent must LOG a compliance gap but still complete registration — never lock
    a user out (the recorded rows are the source of truth; this is an audit surface only)."""
    config.DB_PATH = str(tmp_path / "consent.db")
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    async def go():
        await db.init_db()
        await db.set_setting("consent_enabled", "on")
        await db.set_setting("consent_list", "Обработка ПД|pd")
        await db.set_setting("full_approval", "manual")  # stays pending → no bot sends
        uid = 800900
        msg = _FMsg(uid)
        state = _FState({"full_name": "No Consent", "participant_type": "full"})
        await reg.finalize_registration(msg, state, bot=None)  # user recorded NO consent
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        return await db.get_user(uid)

    row = asyncio.run(go())
    assert row is not None  # registration completed despite the missing consent (non-blocking)
