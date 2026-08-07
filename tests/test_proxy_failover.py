"""Regression tests for services.proxy_session (failover proxy chain for the Telegram
bot session). No real network — AiohttpSession.make_request is monkeypatched with a fake
transport that reads the session's *current* chain index to decide success/failure.

Convention (pytest-asyncio is NOT installed in this project): plain def test_*,
asyncio.run(go()), monkeypatch. Fake bot follows tests/test_sheets_admin_alert.py's
_FakeBot pattern.
"""
import asyncio
import logging

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

import services.proxy_session as proxy_session
from services.proxy_session import (
    FailoverAiohttpSession,
    build_proxy_chain,
    mask_proxy_url,
)

PRIMARY = "http://primary:8080"
BACKUP = "http://backup:8080"


class _Clock:
    """Fake monotonic time source — advanced explicitly, no real sleep."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fake_transport(dead_indexes, calls):
    """Reads self._index (the link the FailoverAiohttpSession currently has applied) at
    call time and either raises TelegramNetworkError (dead) or returns a sentinel result
    (alive). `calls` records the index seen on each invocation."""

    async def fake(self, bot, method, timeout=None):
        idx = self._index
        calls.append(idx)
        if idx in dead_indexes:
            raise TelegramNetworkError(method=method, message="simulated")
        return f"result-{idx}"

    return fake


class _FakeBot:
    def __init__(self, raise_on_send=False):
        self.sent = []
        self.raise_on_send = raise_on_send

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.raise_on_send:
            raise RuntimeError("simulated send failure")


async def _drain_background():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def _reset_alert_state():
    proxy_session.set_alert_bot(None)
    proxy_session._alert_bot_warned = False
    proxy_session._last_alerted_index = None


@pytest.fixture(autouse=True)
def _isolate_proxy_alert_module_state():
    """`_last_alerted_index`/`_alert_bot`/`_alert_bot_warned` are module-level globals
    (mirrors services/sheets.py's `_alert_bot` pattern). Without this autouse reset, a
    rotation in an EARLIER test (e.g. Test 1-4, which never touch the alert bot) leaves
    `_last_alerted_index` set, silently swallowing the dedup-relevant alert in a LATER test
    that reuses the same chain order."""
    _reset_alert_state()
    yield
    _reset_alert_state()


# ── Test 1: rotate ──────────────────────────────────────────────────────────

def test_rotate_primary_dead_backup_alive(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        result = await session.make_request(object(), object())
        return session, result

    session, result = asyncio.run(go())

    assert result == "result-1"
    assert calls == [0, 1]
    assert session.active_index == 1


# ── Test 2: all dead ─────────────────────────────────────────────────────────

def test_all_dead_raises_original_first_error(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0, 1}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        try:
            await session.make_request(object(), object())
            return None, None
        except TelegramNetworkError as e:
            return session, e

    session, raised = asyncio.run(go())

    assert raised is not None
    assert isinstance(raised, TelegramNetworkError)
    assert calls == [0, 1]


def test_all_dead_raises_the_exact_first_exception_object(monkeypatch):
    """Separate from the previous test: captures the FIRST raised exception directly from
    the fake transport and proves identity (not just type) with what make_request raises."""
    calls = []
    first_holder = {}

    async def fake(self, bot, method, timeout=None):
        idx = self._index
        calls.append(idx)
        err = TelegramNetworkError(method=method, message=f"simulated-{idx}")
        if idx == 0:
            first_holder["err"] = err
        raise err

    monkeypatch.setattr(AiohttpSession, "make_request", fake)

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        try:
            await session.make_request(object(), object())
        except TelegramNetworkError as e:
            return e

    raised = asyncio.run(go())
    assert raised is first_holder["err"]
    assert calls == [0, 1]


# ── Test 3: sticky ───────────────────────────────────────────────────────────

def test_sticky_after_rotation_second_request_stays_on_backup(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        await session.make_request(object(), object())  # rotates to backup
        calls.clear()
        result = await session.make_request(object(), object())
        return session, result

    session, result = asyncio.run(go())

    assert result == "result-1"
    assert calls == [1]  # single call, straight to backup, no primary probe
    assert session.active_index == 1


# ── Test 4: recheck ──────────────────────────────────────────────────────────

def test_recheck_after_interval_retries_primary_first(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    clock = _Clock()

    async def go():
        session = FailoverAiohttpSession(
            [PRIMARY, BACKUP], recheck_seconds=600, time_source=clock
        )
        await session.make_request(object(), object())  # rotates to backup
        clock.advance(601)
        calls.clear()
        result = await session.make_request(object(), object())
        return session, result

    session, result = asyncio.run(go())

    assert calls[0] == 0  # first transport call of the second request probes primary
    assert result == "result-1"  # primary still dead -> falls through to backup
    assert session.active_index == 1


def test_recheck_before_interval_stays_sticky(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    clock = _Clock()

    async def go():
        session = FailoverAiohttpSession(
            [PRIMARY, BACKUP], recheck_seconds=600, time_source=clock
        )
        await session.make_request(object(), object())  # rotates to backup
        clock.advance(10)  # well under recheck_seconds
        calls.clear()
        result = await session.make_request(object(), object())
        return session, result

    session, result = asyncio.run(go())

    assert calls == [1]  # no primary probe yet
    assert result == "result-1"


# ── Test 5: single link ──────────────────────────────────────────────────────

def test_single_link_no_rotation_exception_propagates_as_is(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY])
        try:
            await session.make_request(object(), object())
        except TelegramNetworkError as e:
            return e
        return None

    raised = asyncio.run(go())

    assert raised is not None
    assert calls == [0]  # exactly one attempt, no retry loop at all


# ── Test 6: chain builder ────────────────────────────────────────────────────

def test_build_proxy_chain_drops_empty_and_none():
    assert build_proxy_chain(PRIMARY, None, "", "   ") == [PRIMARY]


def test_build_proxy_chain_direct_keywords_become_none():
    assert build_proxy_chain("direct") == [None]
    assert build_proxy_chain("none") == [None]
    assert build_proxy_chain("DIRECT") == [None]


def test_build_proxy_chain_dedup_preserves_order():
    assert build_proxy_chain(PRIMARY, BACKUP, PRIMARY) == [PRIMARY, BACKUP]


def test_build_proxy_chain_all_empty_yields_single_none_link():
    assert build_proxy_chain() == [None]
    assert build_proxy_chain(None, "", "  ") == [None]


def test_build_proxy_chain_preserves_priority_order():
    assert build_proxy_chain(BACKUP, PRIMARY) == [BACKUP, PRIMARY]


# ── mask_proxy_url (used directly by Test 7, sanity-checked here) ───────────

def test_mask_proxy_url_masks_credentials():
    assert mask_proxy_url("socks5://user:secretpass@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"
    assert mask_proxy_url("user:pass@host:1080") == "***@host:1080"


def test_mask_proxy_url_no_credentials_unchanged():
    assert mask_proxy_url("http://1.2.3.4:8118") == "http://1.2.3.4:8118"


def test_mask_proxy_url_none_is_direct():
    assert mask_proxy_url(None) == "direct"


# ── Test 7: masking in log + alert ───────────────────────────────────────────

def test_rotation_alert_and_log_mask_credentials(monkeypatch, caplog):
    calls = []
    secret_primary = "socks5://user:secretpass@1.2.3.4:1080"
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    fake_bot = _FakeBot()
    proxy_session.set_alert_bot(fake_bot)
    caplog.set_level(logging.WARNING, logger="services.proxy_session")
    try:
        async def go():
            session = FailoverAiohttpSession([secret_primary, BACKUP])
            await session.make_request(object(), object())
            await _drain_background()

        asyncio.run(go())
    finally:
        _reset_alert_state()

    assert "secretpass" not in caplog.text
    assert "***@" in caplog.text
    assert fake_bot.sent
    for _chat_id, text in fake_bot.sent:
        assert "secretpass" not in text
        assert "***@" in text


# ── Test 8: alert is fail-soft ────────────────────────────────────────────────

def test_alert_fail_soft_when_no_bot_set(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    _reset_alert_state()  # explicit: no alert bot configured (backfill script scenario)

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        result = await session.make_request(object(), object())
        await _drain_background()
        return result

    result = asyncio.run(go())
    assert result == "result-1"


def test_alert_fail_soft_when_send_message_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    fake_bot = _FakeBot(raise_on_send=True)
    proxy_session.set_alert_bot(fake_bot)
    try:
        async def go():
            session = FailoverAiohttpSession([PRIMARY, BACKUP])
            result = await session.make_request(object(), object())
            await _drain_background()
            return result

        result = asyncio.run(go())
    finally:
        _reset_alert_state()

    assert result == "result-1"  # request still succeeds despite send_message raising


# ── Test 9: dedup ─────────────────────────────────────────────────────────────

def test_dedup_two_rotations_to_different_links_send_two_alerts(monkeypatch):
    calls = []
    THIRD = "http://third:8080"
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0, 1}, calls))
    fake_bot = _FakeBot()
    proxy_session.set_alert_bot(fake_bot)
    try:
        async def go():
            session = FailoverAiohttpSession([PRIMARY, BACKUP, THIRD])
            result = await session.make_request(object(), object())
            await _drain_background()
            return session, result

        session, result = asyncio.run(go())
    finally:
        _reset_alert_state()

    assert result == "result-2"
    assert len(fake_bot.sent) == 2  # one alert per distinct rotation target


def test_dedup_stale_observed_index_sends_no_extra_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    fake_bot = _FakeBot()
    proxy_session.set_alert_bot(fake_bot)
    try:
        async def go():
            session = FailoverAiohttpSession([PRIMARY, BACKUP])
            await session.make_request(object(), object())  # rotates 0 -> 1, one alert
            await _drain_background()
            await session._rotate_from(0)  # stale: session is already at index 1
            await _drain_background()
            return session

        session = asyncio.run(go())
    finally:
        _reset_alert_state()

    assert len(fake_bot.sent) == 1
    assert session.active_index == 1
