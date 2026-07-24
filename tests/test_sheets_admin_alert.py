"""P0 audit T-dw1-02: exhausted-retry admin alert tests for both Sheets append paths.

Follows tests/test_sheets_phase5.py conventions: plain def test_*, asyncio.run(go()),
monkeypatch, and sheets.asyncio.sleep patched to a no-op so the retry backoff is instant.
"""
import asyncio

from config import config
import services.sheets as sheets


def _configure_sheet_creds(monkeypatch):
    """Non-empty credentials so the retry loop actually runs (mirrors
    test_append_to_named_sheet_swallows_raising_sync_call)."""
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")


def _fast_sleep(monkeypatch):
    async def fast_sleep(_):
        return None

    monkeypatch.setattr(sheets.asyncio, "sleep", fast_sleep)


class _FakeBot:
    def __init__(self, raise_on_send=False):
        self.sent = []
        self.raise_on_send = raise_on_send

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.raise_on_send:
            raise RuntimeError("simulated send failure")


def _reset_alert_bot():
    sheets.set_alert_bot(None)


# ── append_to_sheet ───────────────────────────────────────────────────────────

def test_append_to_sheet_alerts_all_admins_on_exhausted_retries(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_sheet_sync", boom)

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)
    try:
        async def go():
            await sheets.append_to_sheet([123, "Test"])

        asyncio.run(go())  # must not raise despite every retry attempt failing
        assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]
    finally:
        _reset_alert_bot()


def test_append_to_sheet_no_alert_on_success(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def ok(data):
        return None

    monkeypatch.setattr(sheets, "_append_to_sheet_sync", ok)

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)
    try:
        async def go():
            await sheets.append_to_sheet([123, "Test"])

        asyncio.run(go())
        assert fake_bot.sent == []
    finally:
        _reset_alert_bot()


def test_append_to_sheet_exhausted_retries_no_bot_set_stays_fail_soft(monkeypatch):
    """set_alert_bot(None) — no alert bot configured — the append must still complete
    without raising."""
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_sheet_sync", boom)

    sheets.set_alert_bot(None)
    try:
        async def go():
            await sheets.append_to_sheet([123, "Test"])

        asyncio.run(go())  # must not raise
    finally:
        _reset_alert_bot()


def test_append_to_sheet_alert_send_failure_swallowed_both_admins_attempted(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_sheet_sync", boom)

    fake_bot = _FakeBot(raise_on_send=True)
    sheets.set_alert_bot(fake_bot)
    try:
        async def go():
            await sheets.append_to_sheet([123, "Test"])

        asyncio.run(go())  # must not raise even though every send_message raises
        assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]
    finally:
        _reset_alert_bot()


# ── append_to_named_sheet ──────────────────────────────────────────────────────

def test_append_to_named_sheet_alerts_all_admins_on_exhausted_retries(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(tab_name, data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_named_sheet_sync", boom)

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)
    try:
        async def go():
            await sheets.append_to_named_sheet("Party", [123, "Test"])

        asyncio.run(go())
        assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]
    finally:
        _reset_alert_bot()


def test_append_to_named_sheet_no_alert_on_success(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def ok(tab_name, data):
        return None

    monkeypatch.setattr(sheets, "_append_to_named_sheet_sync", ok)

    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)
    try:
        async def go():
            await sheets.append_to_named_sheet("Party", [123, "Test"])

        asyncio.run(go())
        assert fake_bot.sent == []
    finally:
        _reset_alert_bot()


def test_append_to_named_sheet_exhausted_retries_no_bot_set_stays_fail_soft(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(tab_name, data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_named_sheet_sync", boom)

    sheets.set_alert_bot(None)
    try:
        async def go():
            await sheets.append_to_named_sheet("Party", [123, "Test"])

        asyncio.run(go())  # must not raise
    finally:
        _reset_alert_bot()


def test_append_to_named_sheet_alert_send_failure_swallowed_both_admins_attempted(monkeypatch):
    _configure_sheet_creds(monkeypatch)
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(tab_name, data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_named_sheet_sync", boom)

    fake_bot = _FakeBot(raise_on_send=True)
    sheets.set_alert_bot(fake_bot)
    try:
        async def go():
            await sheets.append_to_named_sheet("Party", [123, "Test"])

        asyncio.run(go())
        assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]
    finally:
        _reset_alert_bot()
