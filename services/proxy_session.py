"""Failover Telegram-session proxy chain.

Incident 2026-08-06: the bot's single proxy (PROXY_URL -> privoxy -> SOCKS5 -> ssh tunnel)
died and the whole bot went down with it, because the proxy is baked into the aiogram
session once at startup (main.py). FailoverAiohttpSession adds a backup link: on a
TelegramNetworkError it hot-swaps the connector to the next link in the chain (no Bot
re-creation), stays sticky on the working link, and periodically re-probes the primary.

If every link is dead, the ORIGINAL TelegramNetworkError from the first link is re-raised
unchanged so dp.start_polling's existing backoff/retry behaviour is untouched.
"""
import asyncio
import logging
import re
import time

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

logger = logging.getLogger(__name__)

# Matches an optional "scheme://" followed by "user:pass@" (or "user@") right before the
# host. Only touches the credentials segment -- host/port/path are left untouched.
_CRED_RE = re.compile(r"^(\w+://)?[^/@]*@")


def mask_proxy_url(value) -> str:
    """Mask credentials in a proxy URL for logs/alerts. `None` -> "direct" (no proxy).
    Pure function, no network/side effects."""
    if value is None:
        return "direct"
    s = value if isinstance(value, str) else str(value)
    return _CRED_RE.sub(lambda m: (m.group(1) or "") + "***@", s)


def build_proxy_chain(*values) -> list:
    """Build an ordered, deduplicated proxy chain from raw config values (already-unwrapped
    strings or None -- SecretStr.get_secret_value() is the caller's job). Order = priority
    (first = primary). "direct"/"none" (any case) become an explicit None (no-proxy) link.
    Empty/falsy values are dropped. An entirely empty result becomes a single [None] link
    (direct connection), matching today's "no PROXY_URL set" behaviour."""
    chain: list = []
    for v in values:
        if v is None:
            continue
        s = v.strip() if isinstance(v, str) else str(v).strip()
        if not s:
            continue
        item = None if s.lower() in ("direct", "none") else s
        if item not in chain:
            chain.append(item)
    return chain or [None]


class FailoverAiohttpSession(AiohttpSession):
    """AiohttpSession subclass that rotates through a chain of proxy links (or a direct
    "no proxy" link) on TelegramNetworkError, staying sticky on whichever link last worked,
    and periodically retrying the primary link."""

    def __init__(self, chain, recheck_seconds: int = 600, time_source=time.monotonic, **kwargs):
        # Deliberately no `proxy=` kwarg here -- the base __init__ then leaves the plain
        # TCPConnector/certifi-ssl setup untouched, which we snapshot below as the "direct"
        # link. This is the only way to get back to a proxy-less connector later.
        super().__init__(**kwargs)
        self._direct_connector_type = self._connector_type
        self._direct_connector_init = dict(self._connector_init)

        self._chain = list(chain) or [None]
        self._index = 0
        self._switched_at = None
        self._recheck_seconds = recheck_seconds
        self._time_source = time_source
        # Python 3.10+ binds asyncio.Lock to the running loop lazily on first use, so
        # constructing it here (outside a running loop, e.g. at module import time in
        # main.py) is safe.
        self._rotate_lock = asyncio.Lock()
        self._apply(0)

    def _apply(self, index: int) -> None:
        target = self._chain[index]
        if target is None:
            # Restore the pristine direct (no-proxy) connector snapshot.
            self._connector_type = self._direct_connector_type
            self._connector_init = dict(self._direct_connector_init)
            self._proxy = None
            self._should_reset_connector = True
        else:
            self.proxy = target  # base class setter: _setup_proxy_connector + reset flag
        self._index = index

    @property
    def active_index(self) -> int:
        return self._index

    @property
    def active_proxy(self) -> str:
        return mask_proxy_url(self._chain[self._index])

    async def _rotate_from(self, observed_index: int) -> None:
        """Idempotent rotation: if another coroutine already rotated past `observed_index`,
        this is a no-op -- the caller simply retries on the link that's already active."""
        async with self._rotate_lock:
            if self._index != observed_index:
                return
            nxt = (observed_index + 1) % len(self._chain)
            prev_masked = mask_proxy_url(self._chain[observed_index])
            self._apply(nxt)
            self._switched_at = None if nxt == 0 else self._time_source()
            logger.warning(
                "Proxy failover: %s -> %s", prev_masked, mask_proxy_url(self._chain[nxt])
            )

    async def _maybe_return_to_primary(self) -> None:
        if self._index == 0 or self._recheck_seconds <= 0 or self._switched_at is None:
            return
        if self._time_source() - self._switched_at < self._recheck_seconds:
            return
        async with self._rotate_lock:
            # Double-checked: another coroutine may have already returned to primary (or
            # rotated further) while we were waiting for the lock.
            if self._index == 0 or self._recheck_seconds <= 0 or self._switched_at is None:
                return
            if self._time_source() - self._switched_at < self._recheck_seconds:
                return
            self._apply(0)
            self._switched_at = None
            logger.info("Proxy recheck: retrying primary %s", mask_proxy_url(self._chain[0]))

    async def make_request(self, bot, method, timeout=None):
        if len(self._chain) == 1:
            # Zero behavioural difference from the base class with a single link (parity
            # with pre-failover code when only PROXY_URL, or nothing, is set).
            return await super().make_request(bot, method, timeout=timeout)

        await self._maybe_return_to_primary()

        first_error = None
        attempts = 0
        while True:
            current = self._index
            try:
                return await super().make_request(bot, method, timeout=timeout)
            except TelegramNetworkError as e:
                if first_error is None:
                    first_error = e
                attempts += 1
                if attempts >= len(self._chain):
                    # Full circle, nothing alive -- surface the ORIGINAL error so
                    # dp.start_polling's existing backoff/retry logic is unaffected.
                    raise first_error
                await self._rotate_from(current)
