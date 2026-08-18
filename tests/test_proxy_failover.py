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
from aiohttp import ClientTimeout
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


def _timeout_capturing_transport(captured):
    """Records the `timeout` kwarg it was called with (one entry per call) and returns a
    sentinel result -- used to assert what FailoverAiohttpSession.make_request forwards to
    the base class, independent of the dead/alive routing that _fake_transport tests."""

    async def fake(self, bot, method, timeout=None):
        captured.append(timeout)
        return "result"

    return fake


class _ProbeSession(FailoverAiohttpSession):
    """Test subclass that overrides _probe_primary to pop scripted results from a list
    instead of touching the network. A scripted result may be an Exception INSTANCE, which
    this override raises (exercising _probe_loop's own try/except around _probe_primary).

    `probed_event`, if given, is `.set()` synchronously right as each attempt starts --
    lets a test do `await evt.wait(); evt.clear()` in a loop for deterministic
    "N attempts have started" synchronization instead of blind asyncio.sleep(0) pumping.
    """

    def __init__(self, *args, probe_results=None, probed_event=None, **kwargs):
        self._probe_results = list(probe_results or [])
        self._probed_event = probed_event
        super().__init__(*args, **kwargs)

    async def _probe_primary(self) -> bool:
        result = self._probe_results.pop(0) if self._probe_results else False
        if self._probed_event is not None:
            self._probed_event.set()
        if isinstance(result, Exception):
            raise result
        return result


class _FakeSleep:
    """Records requested delays and returns (near-)immediately -- used as sleep_func so
    _probe_loop's `await self._sleep(recheck_seconds)` doesn't actually block the test.

    Still does a real `asyncio.sleep(0)` checkpoint: none of the fakes in this module
    (this sleep, _ProbeSession._probe_primary) touch real I/O, so without an explicit
    checkpoint the probe loop would spin as a tight, never-yielding loop once its scripted
    results run out (`_probe_primary` -> False forever) and would starve the test's own
    coroutine instead of interleaving with it.
    """

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


class _FakeBot:
    def __init__(self, raise_on_send=False):
        self.sent = []
        self.raise_on_send = raise_on_send

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.raise_on_send:
            raise RuntimeError("simulated send failure")


async def _drain_background():
    """Await all fire-and-forget background tasks spawned by the test (admin alert sends).

    The background primary-probe loop (_probe_loop) is long-lived by design -- it only
    exits once it returns to primary -- so gathering it unconditionally would hang the
    whole test. Cancel any pending probe-loop tasks first (identified by coroutine
    __qualname__, since they're not otherwise distinguishable from alert tasks), then
    gather everything else normally.
    """
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        if getattr(t.get_coro(), "__qualname__", "").endswith("_probe_loop"):
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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


# ── Test 4: recheck moved off the live path (background probe) ─────────────────

def test_live_requests_never_probe_primary_after_recheck_interval(monkeypatch):
    """INVERTS the old (pre-background-probe) assertion on purpose. Prod 17.08: a live
    request must never be spent testing a known-dead primary link -- return-to-primary now
    happens EXCLUSIVELY via the background probe (see the Test 12 block below), never on
    the live request path, no matter how much simulated time has passed."""
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    clock = _Clock()

    async def go():
        session = FailoverAiohttpSession(
            [PRIMARY, BACKUP], recheck_seconds=600, time_source=clock
        )
        await session.make_request(object(), object())  # rotates to backup
        clock.advance(601)  # past recheck_seconds -- irrelevant now, no live-path gate
        calls.clear()
        result = await session.make_request(object(), object())
        return session, result

    session, result = asyncio.run(go())

    assert calls == [1]  # straight to backup -- primary never touched on the live path
    assert result == "result-1"
    assert session.active_index == 1


def test_recheck_before_interval_stays_sticky(monkeypatch):
    """Still valid post-background-probe: with no time advanced at all, the live request
    goes straight to backup regardless of whether a probe is separately running."""
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

    assert calls == [1]  # no primary probe on the live path
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


# ── Test 10: PROXY_CONNECT_TIMEOUT bounds connection setup ─────────────────────

def test_explicit_timeout_forwarded_as_client_timeout_with_connect_bound(monkeypatch):
    captured = []
    monkeypatch.setattr(AiohttpSession, "make_request", _timeout_capturing_transport(captured))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP], connect_timeout=5)
        await session.make_request(object(), object(), timeout=90)

    asyncio.run(go())

    assert len(captured) == 1
    ct = captured[0]
    assert isinstance(ct, ClientTimeout)
    assert ct.total == 90
    assert ct.connect == 5
    assert ct.sock_connect == 5


def test_none_timeout_forwarded_as_client_timeout_using_session_default(monkeypatch):
    captured = []
    monkeypatch.setattr(AiohttpSession, "make_request", _timeout_capturing_transport(captured))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP], connect_timeout=5)
        await session.make_request(object(), object(), timeout=None)
        return session

    session = asyncio.run(go())

    ct = captured[0]
    assert isinstance(ct, ClientTimeout)
    assert ct.total == session.timeout
    assert ct.connect == 5
    assert ct.sock_connect == 5


def test_single_link_parity_branch_also_gets_connect_bound(monkeypatch):
    captured = []
    monkeypatch.setattr(AiohttpSession, "make_request", _timeout_capturing_transport(captured))

    async def go():
        session = FailoverAiohttpSession([PRIMARY], connect_timeout=5)
        await session.make_request(object(), object(), timeout=90)

    asyncio.run(go())

    ct = captured[0]
    assert isinstance(ct, ClientTimeout)
    assert ct.total == 90
    assert ct.connect == 5
    assert ct.sock_connect == 5


def test_connect_timeout_zero_forwards_original_value_untouched(monkeypatch):
    captured = []
    monkeypatch.setattr(AiohttpSession, "make_request", _timeout_capturing_transport(captured))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP], connect_timeout=0)
        await session.make_request(object(), object(), timeout=90)

    asyncio.run(go())

    assert captured[0] == 90  # untouched -- no ClientTimeout wrap


def test_connect_timeout_negative_forwards_original_value_untouched(monkeypatch):
    captured = []
    monkeypatch.setattr(AiohttpSession, "make_request", _timeout_capturing_transport(captured))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP], connect_timeout=-1)
        await session.make_request(object(), object(), timeout=None)

    asyncio.run(go())

    assert captured[0] is None  # untouched -- no ClientTimeout wrap


def test_session_timeout_stays_numeric_for_dispatcher_arithmetic():
    """Guards aiogram dispatcher.py:216: `int(bot.session.timeout + polling_timeout)`.
    Assigning a ClientTimeout to self.timeout anywhere would crash long polling at
    startup -- this is an import/construction-level smoke check, not a make_request test."""
    session = FailoverAiohttpSession([PRIMARY, BACKUP], connect_timeout=5)
    assert isinstance(int(session.timeout + 30), int)


# ── Test 11: failover cause logging ─────────────────────────────────────────

def test_warning_line_names_the_underlying_network_error(monkeypatch, caplog):
    calls = []

    async def fake(self, bot, method, timeout=None):
        idx = self._index
        calls.append(idx)
        if idx == 0:
            raise TelegramNetworkError(method=method, message="tunnel closed")
        return f"result-{idx}"

    monkeypatch.setattr(AiohttpSession, "make_request", fake)
    caplog.set_level(logging.WARNING, logger="services.proxy_session")

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        await session.make_request(object(), object())
        await _drain_background()

    asyncio.run(go())

    assert "cause: TelegramNetworkError" in caplog.text
    assert "tunnel closed" in caplog.text


def test_admin_alert_carries_the_same_cause_on_its_own_line(monkeypatch):
    calls = []

    async def fake(self, bot, method, timeout=None):
        idx = self._index
        calls.append(idx)
        if idx == 0:
            raise TelegramNetworkError(method=method, message="tunnel closed")
        return f"result-{idx}"

    monkeypatch.setattr(AiohttpSession, "make_request", fake)
    fake_bot = _FakeBot()
    proxy_session.set_alert_bot(fake_bot)
    try:
        async def go():
            session = FailoverAiohttpSession([PRIMARY, BACKUP])
            await session.make_request(object(), object())
            await _drain_background()

        asyncio.run(go())
    finally:
        _reset_alert_state()

    assert fake_bot.sent
    _chat_id, text = fake_bot.sent[0]
    assert "Причина:" in text
    assert "tunnel closed" in text


def test_failover_cause_scrubs_credentials_from_log_and_alert(monkeypatch, caplog):
    calls = []

    async def fake(self, bot, method, timeout=None):
        idx = self._index
        calls.append(idx)
        if idx == 0:
            raise TelegramNetworkError(
                method=method,
                message=(
                    "ClientProxyConnectionError: Cannot connect to "
                    "socks5://user:secretpass@1.2.3.4:1080"
                ),
            )
        return f"result-{idx}"

    monkeypatch.setattr(AiohttpSession, "make_request", fake)
    fake_bot = _FakeBot()
    proxy_session.set_alert_bot(fake_bot)
    caplog.set_level(logging.WARNING, logger="services.proxy_session")
    try:
        async def go():
            session = FailoverAiohttpSession([PRIMARY, BACKUP])
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


def test_rotate_from_with_no_error_argument_logs_unknown_cause(caplog):
    """Direct call with observed_index matching the CURRENT index (no live request
    involved) -- this is the "no error object available" path, e.g. a caller other than
    make_request's except branch. Distinct from the stale-observed-index dedup test below,
    which exercises the early-return no-op branch instead."""
    caplog.set_level(logging.WARNING, logger="services.proxy_session")

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP])
        await session._rotate_from(0)  # observed_index == current index (0) -> rotates
        await _drain_background()
        return session

    session = asyncio.run(go())

    assert "cause: unknown" in caplog.text
    assert session.active_index == 1


# ── Test 12: background probe returns to primary, never on a live request ──────

def test_probe_loop_returns_to_primary_only_after_probe_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    fake_sleep = _FakeSleep()

    async def go():
        session = _ProbeSession(
            [PRIMARY, BACKUP],
            recheck_seconds=600,
            sleep_func=fake_sleep,
            probe_results=[False, False, True],
        )
        await session.make_request(object(), object())  # rotates to backup, starts probe
        # Deterministic: the task self-terminates the moment it returns to primary.
        await session._probe_task
        return session

    session = asyncio.run(go())

    assert session.active_index == 0
    assert fake_sleep.delays == [600, 600, 600]  # one sleep per probe attempt


def test_probe_loop_keeps_retrying_while_probe_fails_or_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    fake_sleep = _FakeSleep()
    probed = asyncio.Event()

    async def go():
        session = _ProbeSession(
            [PRIMARY, BACKUP],
            recheck_seconds=600,
            sleep_func=fake_sleep,
            probe_results=[False, RuntimeError("boom"), False],
            probed_event=probed,
        )
        await session.make_request(object(), object())  # rotates to backup, starts probe
        for _ in range(3):
            await probed.wait()
            probed.clear()
        # One more scheduling slot so the loop processes the 3rd result and loops back.
        await asyncio.sleep(0)
        return session

    session = asyncio.run(go())

    assert session.active_index == 1  # stayed on backup -- probe never succeeded
    assert len(fake_sleep.delays) >= 3  # kept retrying past both False and the exception


def test_interleaved_live_requests_stay_on_backup_while_probe_keeps_failing(monkeypatch):
    """Live requests and a concurrently-running (always-failing) background probe must
    never interact: every live request still goes straight to the backup index."""
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))
    fake_sleep = _FakeSleep()
    probed = asyncio.Event()

    async def go():
        session = _ProbeSession(
            [PRIMARY, BACKUP],
            recheck_seconds=600,
            sleep_func=fake_sleep,
            probe_results=[False, False],
            probed_event=probed,
        )
        await session.make_request(object(), object())  # rotates to backup, starts probe
        calls.clear()
        for _ in range(2):
            await probed.wait()
            probed.clear()
            await session.make_request(object(), object())
        return session

    session = asyncio.run(go())

    assert calls == [1, 1]  # every live request during the probe window hit backup only
    assert session.active_index == 1


def test_recheck_seconds_zero_starts_no_background_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP], recheck_seconds=0)
        await session.make_request(object(), object())  # rotates to backup
        return session

    session = asyncio.run(go())

    assert session._probe_task is None
    assert session.active_index == 1  # stays on backup indefinitely -- parity w/ before


def test_single_link_chain_never_starts_a_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY])
        try:
            await session.make_request(object(), object())
        except TelegramNetworkError:
            pass
        return session

    session = asyncio.run(go())

    assert session._probe_task is None


def test_ensure_probe_task_twice_yields_the_same_task(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, BACKUP], recheck_seconds=600)
        await session.make_request(object(), object())  # rotates to backup, starts probe
        first_task = session._probe_task
        session._ensure_probe_task()
        second_task = session._probe_task
        await _drain_background()
        return first_task, second_task

    first_task, second_task = asyncio.run(go())

    assert first_task is not None
    assert first_task is second_task  # never two concurrent loops


def test_probe_timeout_uses_connect_timeout_for_connect_and_sock_connect():
    session = FailoverAiohttpSession([PRIMARY, BACKUP], connect_timeout=5)
    ct = session._probe_timeout()

    assert isinstance(ct, ClientTimeout)
    assert ct.connect == 5
    assert ct.sock_connect == 5
    assert ct.total >= ct.connect  # bounded -- a dead link costs ~5s, not the full 90s


# ── ApiLink: «api:https://host» = без прокси, другой хост Bot API (Cloudflare Worker) ──

WORKER = "https://tg.example.com"


def test_build_proxy_chain_api_prefix_becomes_api_link():
    chain = build_proxy_chain(PRIMARY, "api:" + WORKER + "/")
    assert chain == [PRIMARY, proxy_session.ApiLink(WORKER)]
    # Case-insensitive prefix, trailing slash stripped, duplicates collapsed.
    assert build_proxy_chain("API:" + WORKER, "api:" + WORKER + "/") == [
        proxy_session.ApiLink(WORKER)
    ]
    # Empty base is dropped like an empty value.
    assert build_proxy_chain("api:", PRIMARY) == [PRIMARY]


def test_mask_proxy_url_api_link_is_direct_arrow_base():
    assert mask_proxy_url(proxy_session.ApiLink(WORKER)) == "direct→" + WORKER


def test_api_link_session_uses_worker_base_and_direct_connector():
    session = FailoverAiohttpSession([proxy_session.ApiLink(WORKER), PRIMARY])
    assert session.api.api_url("T", "getMe") == WORKER + "/botT/getMe"
    assert session.api.file_url("T", "a/b") == WORKER + "/file/botT/a/b"
    assert session._proxy is None
    # Probe of an ApiLink primary targets the Worker, not api.telegram.org.
    assert session._probe_url() == WORKER


def test_default_chain_probe_url_is_telegram():
    session = FailoverAiohttpSession([PRIMARY, BACKUP])
    assert session._probe_url() == "https://api.telegram.org"
    assert session.api.api_url("T", "getMe") == "https://api.telegram.org/botT/getMe"


def test_rotation_to_api_link_swaps_api_and_back(monkeypatch):
    calls = []
    monkeypatch.setattr(AiohttpSession, "make_request", _fake_transport({0}, calls))

    async def go():
        session = FailoverAiohttpSession([PRIMARY, proxy_session.ApiLink(WORKER)])
        before = session.api.api_url("T", "getMe")
        await session.make_request(object(), object())
        after = session.api.api_url("T", "getMe")
        proxy_after = session._proxy
        session._apply(0)
        restored = session.api.api_url("T", "getMe")
        await _drain_background()
        return before, after, proxy_after, restored, session._proxy

    before, after, proxy_after, restored, proxy_restored = asyncio.run(go())
    assert before == "https://api.telegram.org/botT/getMe"
    assert after == WORKER + "/botT/getMe"
    assert proxy_after is None
    assert restored == "https://api.telegram.org/botT/getMe"
    assert proxy_restored == PRIMARY
