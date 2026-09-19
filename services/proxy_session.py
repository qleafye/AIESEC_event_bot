"""Failover Telegram-session proxy chain.

Incident 2026-08-06: the bot's single proxy (PROXY_URL -> privoxy -> SOCKS5 -> ssh tunnel)
died and the whole bot went down with it, because the proxy is baked into the aiogram
session once at startup (main.py). FailoverAiohttpSession adds a backup link: on a
TelegramNetworkError it hot-swaps the connector to the next link in the chain (no Bot
re-creation), stays sticky on the working link, and periodically re-probes the primary.

If every link is dead, the ORIGINAL TelegramNetworkError from the first link is re-raised
unchanged so dp.start_polling's existing backoff/retry behaviour is untouched.

Incidents 2026-09-08/2026-09-15 ("proxy storm"): with the backup channel itself dead all of
September, every single TelegramNetworkError rotated the channel again -- primary/backup
bounced 12 times in 55 seconds, and the admin alert fired straight through the very session
that had just failed (all 156 alert attempts across both storms logged "ERROR ... failed";
admins never heard about either storm). Two fixes below:

- `_rotate_from` now enforces a minimum dwell (`dwell_seconds`, registry
  `proxy_switch_dwell_seconds`) between real rotations: a channel gets that many seconds to
  prove itself before the bot concedes it's dead and hops again. While dwell is blocking a
  hop, the failing request just fails/retries the ordinary way (dp.start_polling's own
  backoff) -- nothing here changes that path.
- Admin alerts move off the live rotation path entirely: `_flap_alert_loop` (+ the
  success-path hook in `make_request`) waits for the flap to go quiet -- either the channel
  holds for a full `dwell_seconds` with no further rotation, or a live request succeeds on
  it -- and only THEN spawns one alert summarising the whole episode.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import ClientTimeout
# We already depend on this module's connector plumbing (_connector_type /
# _connector_init / _setup_proxy_connector via the base class) -- _prepare_connector is
# the same private helper AiohttpSession.proxy's setter uses internally, reused here to
# build a throwaway connector for the background primary probe.
from aiogram.client.session.aiohttp import AiohttpSession, _prepare_connector
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError

from config import config
from services.background import spawn
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

# Admin-alert hook, one-in-one-out with services/sheets.py's set_alert_bot pattern (P0 audit
# T-dw1-02): fail-soft, never raises, warns once if no bot was injected (e.g.
# scripts/backfill_resumes.py, which intentionally never calls set_alert_bot).
_alert_bot = None
_alert_bot_warned = False
# Admins who blocked the bot. Same reasoning as services/reminders.py: a blocked admin can
# never receive an alert, so retrying on every rotation just prints an identical ERROR. Noted
# once, then skipped; a restart clears the set and re-tests whether they unblocked.
_blocked_admins: set[int] = set()


def set_alert_bot(bot) -> None:
    global _alert_bot
    _alert_bot = bot


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Pick the correct Russian plural form for a bare count -- «1 переключение» / «2
    переключения» / «5 переключений». Pure function, standard 1/2-4/5-20 rule."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    tail = n_abs % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


async def _alert_admins_proxy_storm(
    from_masked: str,
    to_masked: str,
    switch_count: int,
    episode_start,
    episode_end,
    cause: str = "unknown",
) -> None:
    """Fired via services.background.spawn ONLY after a flap episode stabilises (see
    FailoverAiohttpSession._flap_alert_loop / _stabilize_on_success) -- NEVER straight out
    of a rotation. Incidents 2026-09-08/2026-09-15: firing through the session that had
    JUST failed meant all 156 alert attempts across both storms logged "ERROR ... failed"
    and admins never heard about either storm; waiting for stabilisation first means the
    session used to send this is proven to carry traffic again. Wrapped so this can NEVER
    raise into the caller.

    `episode_start`/`episode_end` are naive Moscow datetimes (services.timeutil.msk_now) --
    wall-clock, independent of the monotonic time_source used for the dwell/quiet-period
    math, since a manager reads "с 21:03 по 21:04", not a duration since epoch.
    """
    global _alert_bot_warned
    try:
        if _alert_bot is None:
            if not _alert_bot_warned:
                logger.warning(
                    "_alert_admins_proxy_storm: no alert bot set, skipping admin alert"
                )
                _alert_bot_warned = True
            return
        if switch_count <= 1:
            # A single, isolated failover -- same wording as the old immediate alert, just
            # sent once the link has proven itself instead of through the failing session.
            text = (
                f"⚠️ Прокси переключился: {from_masked} → {to_masked}. "
                "Бот продолжает работать через резервный канал. "
                "Проверьте основной прокси/туннель."
            )
        else:
            word = _plural_ru(switch_count, "переключение", "переключения", "переключений")
            text = (
                f"⚠️ Прокси штормило: {switch_count} {word} за "
                f"{episode_start:%H:%M}–{episode_end:%H:%M} МСК. "
                f"Сейчас работаем через {to_masked}. "
                "Проверьте основной прокси/туннель."
            )
        # A manager should not read «Причина: unknown» -- only append when we actually
        # have a diagnostic cause (e.g. _rotate_from(observed_index) called with no error,
        # like the stale-index dedup path).
        if cause != "unknown":
            text += f"\nПричина: {cause}"
        for admin_id in config.ADMIN_IDS:
            if admin_id in _blocked_admins:
                continue
            try:
                await _alert_bot.send_message(admin_id, text)
            except TelegramForbiddenError as e:
                _blocked_admins.add(admin_id)
                logger.warning(
                    f"Proxy storm alert: admin {admin_id} blocked the bot — "
                    f"muting alerts for them until restart: {e}"
                )
            except Exception as e:
                logger.error(f"Proxy storm alert to {admin_id} failed: {e}")
    except Exception as e:
        logger.error(f"_alert_admins_proxy_storm failed: {e}")

# Matches an optional "scheme://" followed by "user:pass@" (or "user@") right before the
# host. Only touches the credentials segment -- host/port/path are left untouched.
_CRED_RE = re.compile(r"^(\w+://)?[^/@]*@")


@dataclass(frozen=True)
class ApiLink:
    """A chain link that carries NO proxy but talks to a different Bot API host -- e.g. a
    Cloudflare Worker reverse-proxying api.telegram.org on the owner's own domain (2026-08-18:
    aeza went dark at the hoster and the bot was left with a single live path). Config
    spelling: `api:https://tg.example.com`. Direct connector + TelegramAPIServer.from_base;
    the Worker is reachable from RU without any tunnel, so this is a third path that shares
    no infrastructure with either VPS."""

    base: str


API_LINK_PREFIX = "api:"
# One config value may carry several links: "http://a:1, api:https://tg.example.com".
# Commas and whitespace never appear unescaped inside a proxy URL, so both are safe separators.
_LINK_SPLIT_RE = re.compile(r"[,\s]+")


def mask_proxy_url(value) -> str:
    """Mask credentials in a proxy URL for logs/alerts. `None` -> "direct" (no proxy),
    ApiLink -> "direct→<base>". Pure function, no network/side effects."""
    if value is None:
        return "direct"
    if isinstance(value, ApiLink):
        return f"direct→{value.base}"
    s = value if isinstance(value, str) else str(value)
    return _CRED_RE.sub(lambda m: (m.group(1) or "") + "***@", s)


# Unlike _CRED_RE (anchored to the START of a standalone URL), an aiohttp/network error
# message embeds the proxy URL MID-STRING (e.g. "Cannot connect to socks5://user:pass@...").
# This one is unanchored and requires a literal "user:pass@" pair, so plain hosts like
# "https://api.telegram.org" are left untouched.
_CAUSE_CRED_RE = re.compile(r"[^\s/@]+:[^\s/@]*@")


def _scrub_credentials(text: str) -> str:
    """Replace any embedded "user:pass@" credential pair with "***@". Pure function."""
    return _CAUSE_CRED_RE.sub("***@", text)


def _describe_error(error) -> str:
    """Render a short, credential-scrubbed description of the exception that triggered a
    proxy failover, for the WARNING log line and the admin alert. `None` -> "unknown"
    (keeps `_rotate_from(observed_index)` called with no error argument meaningful).
    Truncated to 200 chars -- aiohttp connection errors can be paragraph-length and this
    string goes into a Telegram message."""
    if error is None:
        return "unknown"
    text = _scrub_credentials(f"{type(error).__name__}: {error}")
    if len(text) > 200:
        text = text[:200] + "…"
    return text


def build_proxy_chain(*values) -> list:
    """Build an ordered, deduplicated proxy chain from raw config values (already-unwrapped
    strings or None -- SecretStr.get_secret_value() is the caller's job). Order = priority
    (first = primary). Each value may itself hold SEVERAL links separated by commas and/or
    whitespace (so PROXY_URL_BACKUP can list a second and third fallback -- 2026-08-18: two
    slots were not enough once a Cloudflare Worker joined two VPS as a third path).
    "direct"/"none" (any case) become an explicit None (no-proxy) link; "api:https://host"
    becomes an ApiLink (no proxy, alternative Bot API base URL). Empty/falsy values are
    dropped. An entirely empty result becomes a single [None] link (direct connection),
    matching today's "no PROXY_URL set" behaviour."""
    chain: list = []
    parts: list = []
    for v in values:
        if v is None:
            continue
        raw = v if isinstance(v, str) else str(v)
        parts.extend(_LINK_SPLIT_RE.split(raw))
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if s.lower() in ("direct", "none"):
            item = None
        elif s.lower().startswith(API_LINK_PREFIX):
            base = s[len(API_LINK_PREFIX):].strip().rstrip("/")
            if not base:
                continue
            item = ApiLink(base)
        else:
            item = s
        if item not in chain:
            chain.append(item)
    return chain or [None]


class FailoverAiohttpSession(AiohttpSession):
    """AiohttpSession subclass that rotates through a chain of proxy links (or a direct
    "no proxy" link) on TelegramNetworkError, staying sticky on whichever link last worked,
    and periodically retrying the primary link."""

    # Storm guard (2026-09-08/09-15): if a flap episode never goes quiet on its own, don't
    # hammer the stabilisation check every `dwell_seconds` (that IS the storm cadence) --
    # back off to this fixed, generous cadence instead. Not a registry key: this is a pure
    # backstop, not something a manager would ever need to tune per-event.
    _FLAP_ALERT_RETRY_SECONDS = 180

    def __init__(
        self,
        chain,
        recheck_seconds: int = 600,
        time_source=time.monotonic,
        connect_timeout: int = 5,
        sleep_func=None,
        dwell_seconds: int = 30,
        wall_clock_source=None,
        **kwargs,
    ):
        # Deliberately no `proxy=` kwarg here -- the base __init__ then leaves the plain
        # TCPConnector/certifi-ssl setup untouched, which we snapshot below as the "direct"
        # link. This is the only way to get back to a proxy-less connector later.
        super().__init__(**kwargs)
        self._direct_connector_type = self._connector_type
        self._direct_connector_init = dict(self._connector_init)
        # Base-class `api` (PRODUCTION unless the caller passed one). ApiLink links swap
        # self.api per link; every other link restores this.
        self._default_api = self.api

        self._chain = list(chain) or [None]
        self._index = 0
        # Set on every rotation AWAY from primary; kept (though it no longer gates
        # anything on the live path) so the background probe can report how long the bot
        # sat on the backup once it finally returns to primary.
        self._switched_at = None
        self._recheck_seconds = recheck_seconds
        self._time_source = time_source
        # Literal default (not read from config here) so the class stays usable from
        # scripts/tests without touching config. <= 0 disables the bound (escape hatch).
        self._connect_timeout = connect_timeout
        # Testability seam mirroring time_source -- the suite has no pytest-asyncio and
        # must not sleep for real while exercising the background probe loop.
        self._sleep = sleep_func or asyncio.sleep
        self._probe_task: asyncio.Task | None = None
        # Python 3.10+ binds asyncio.Lock to the running loop lazily on first use, so
        # constructing it here (outside a running loop, e.g. at module import time in
        # main.py) is safe.
        self._rotate_lock = asyncio.Lock()

        # Storm guard (2026-09-08/09-15, registry `proxy_switch_dwell_seconds`): minimum
        # time a just-applied link gets before _rotate_from will hop it again. `None` means
        # "never rotated yet" -- the very FIRST failover off primary is always immediate
        # (that's a normal, single incident, not a storm); dwell only starts mattering once
        # at least one rotation has actually happened. <= 0 disables the guard entirely
        # (escape hatch, mirrors recheck_seconds/connect_timeout).
        self._dwell_seconds = dwell_seconds
        self._last_rotate_at: float | None = None
        # Literal default (not read from config -- this is wall-clock text for a human,
        # unrelated to the monotonic time_source used for all the dwell/quiet-period math
        # above), overridable for tests same as time_source/sleep_func.
        self._wall_clock_source = wall_clock_source or msk_now

        # Flap-episode bookkeeping for the ONE delayed admin alert per storm (see
        # _note_flap_switch / _flap_alert_loop / _stabilize_on_success below). An "episode"
        # starts at the first rotation after a quiet period and ends -- exactly once -- when
        # either the active link holds for a full dwell_seconds, or a live request succeeds.
        self._flap_alert_pending = False
        self._flap_switch_count = 0
        self._flap_from_masked: str | None = None
        self._flap_episode_start_wall = None
        self._flap_last_cause = "unknown"
        self._flap_last_switch_at: float | None = None
        self._flap_alert_task: asyncio.Task | None = None

        self._apply(0)

    def _apply(self, index: int) -> None:
        target = self._chain[index]
        if target is None or isinstance(target, ApiLink):
            # Restore the pristine direct (no-proxy) connector snapshot.
            self._connector_type = self._direct_connector_type
            self._connector_init = dict(self._direct_connector_init)
            self._proxy = None
            self._should_reset_connector = True
        else:
            self.proxy = target  # base class setter: _setup_proxy_connector + reset flag
        # Bot API host follows the link: the base class reads self.api on every request
        # (api_url) and file download (file_url), so swapping it here is all that's needed.
        self.api = (
            TelegramAPIServer.from_base(target.base)
            if isinstance(target, ApiLink)
            else self._default_api
        )
        self._index = index

    @property
    def active_index(self) -> int:
        return self._index

    @property
    def active_proxy(self) -> str:
        return mask_proxy_url(self._chain[self._index])

    async def _rotate_from(self, observed_index: int, error=None) -> None:
        """Idempotent rotation: if another coroutine already rotated past `observed_index`,
        this is a no-op -- the caller simply retries on the link that's already active.

        Storm guard (2026-09-08/09-15): if the CURRENT link was applied less than
        `dwell_seconds` ago, this is ALSO a no-op -- the link just switched and gets a
        window to prove itself before the bot concedes it's dead too. The caller's own
        retry loop still runs its course and the request still fails/retries exactly like
        today; only the rotation itself is swallowed. See the module docstring for why this
        exists.

        `error` is the TelegramNetworkError that triggered the rotation (None keeps the
        existing stale-index dedup test's direct `_rotate_from(0)` call meaningful, logging
        `cause: unknown`)."""
        cause = _describe_error(error)
        async with self._rotate_lock:
            if self._index != observed_index:
                return
            now = self._time_source()
            if (
                self._dwell_seconds > 0
                and self._last_rotate_at is not None
                and (now - self._last_rotate_at) < self._dwell_seconds
            ):
                logger.debug(
                    "Proxy failover: dwell active (%.0fs left), not rotating off %s (cause: %s)",
                    self._dwell_seconds - (now - self._last_rotate_at),
                    mask_proxy_url(self._chain[observed_index]),
                    cause,
                )
                return
            nxt = (observed_index + 1) % len(self._chain)
            prev_masked = mask_proxy_url(self._chain[observed_index])
            self._apply(nxt)
            self._last_rotate_at = now
            self._switched_at = None if nxt == 0 else now
            to_masked = mask_proxy_url(self._chain[nxt])
            logger.warning(
                "Proxy failover: %s -> %s (cause: %s)", prev_masked, to_masked, cause
            )
            self._note_flap_switch(prev_masked, cause, now)
            self._ensure_probe_task()

    def _note_flap_switch(self, prev_masked: str, cause: str, now: float) -> None:
        """Must be called with self._rotate_lock held, right after a real rotation applied
        in _rotate_from. Starts a new flap episode (first rotation since the last alert) or
        extends the current one, and arms the background stabilisation wait -- see
        _flap_alert_loop's docstring for how an episode ends and the ONE admin alert for it
        gets sent."""
        if not self._flap_alert_pending:
            self._flap_alert_pending = True
            self._flap_switch_count = 0
            self._flap_from_masked = prev_masked
            self._flap_episode_start_wall = self._wall_clock_source()
        self._flap_switch_count += 1
        self._flap_last_cause = cause
        self._flap_last_switch_at = now
        self._ensure_flap_alert_task()

    def _fire_flap_alert(self) -> None:
        """Must be called with self._rotate_lock held. Snapshots the current flap episode,
        resets the bookkeeping so a LATER flap starts a fresh episode (and gets its own
        alert), and spawns the one admin alert for it. Synchronous/fire-and-forget --
        `spawn()` just schedules the coroutine, so this never blocks whichever caller
        (`_flap_alert_loop` or the make_request success hook) triggered stabilisation."""
        to_masked = mask_proxy_url(self._chain[self._index])
        spawn(
            _alert_admins_proxy_storm(
                self._flap_from_masked,
                to_masked,
                self._flap_switch_count,
                self._flap_episode_start_wall,
                self._wall_clock_source(),
                self._flap_last_cause,
            )
        )
        self._flap_alert_pending = False
        self._flap_switch_count = 0
        self._flap_from_masked = None
        self._flap_episode_start_wall = None
        self._flap_last_cause = "unknown"

    async def _stabilize_on_success(self) -> None:
        """Second of the two stabilisation triggers (see _flap_alert_loop's docstring): a
        live request succeeding on the currently-active link is stronger evidence than a
        bare timer, so it fires the episode's alert right away instead of waiting out the
        rest of dwell_seconds. Cheap no-op when there is no pending episode (the overwhelming
        majority of successful requests -- checked again under the lock since the caller's
        pre-check is lock-free)."""
        if not self._flap_alert_pending:
            return
        async with self._rotate_lock:
            if not self._flap_alert_pending:
                return
            self._fire_flap_alert()

    def _ensure_flap_alert_task(self) -> None:
        """Sync method (mirrors _ensure_probe_task) -- called from inside _rotate_from's
        lock block, must not itself await. Starts the background stabilisation-wait loop if
        one isn't already running for the current episode."""
        if self._flap_alert_task is not None and not self._flap_alert_task.done():
            return
        try:
            self._flap_alert_task = spawn(self._flap_alert_loop())
        except RuntimeError:
            # spawn() needs a running event loop -- see _ensure_probe_task's identical
            # comment. Worst case here: the delayed alert is skipped for this episode, but
            # the NEXT rotation (from within a running loop) re-arms it.
            logger.debug(
                "_ensure_flap_alert_task: no running loop, skipping stabilisation wait"
            )

    async def _flap_alert_loop(self) -> None:
        """Background: waits for the current flap episode to go quiet -- the active link
        holding for a full `dwell_seconds` with no further rotation -- and then fires the
        ONE stabilised-episode admin alert. A live request succeeding stabilises the episode
        immediately instead, via `_stabilize_on_success` (both paths check-and-clear
        `_flap_alert_pending` under the same lock, so only one of them actually sends).

        If the flap keeps going (never quiet for a whole dwell_seconds), retries back off to
        `_FLAP_ALERT_RETRY_SECONDS` instead of re-checking every `dwell_seconds` -- prod
        storms rotate roughly every 5s, dwell defaults to 30s, and checking that often would
        just recreate the noise this whole feature exists to remove.

        Same self-terminating shape as `_probe_loop` (see its docstring for why neither loop
        may be cancelled from `close()`): this loop exits the moment it sends the alert (or
        finds `_flap_alert_pending` already cleared by the success path).

        Checks `_flap_alert_pending` BOTH before and after each sleep (not just after): the
        success path can stabilise the episode synchronously, before this task -- scheduled
        but not yet run -- gets its first turn on the event loop at all. Without the
        up-front check, this loop would burn one pointless `self._sleep(dwell_seconds)` call
        on an episode that is already over.
        """
        wait = self._dwell_seconds
        while True:
            async with self._rotate_lock:
                if not self._flap_alert_pending:
                    return
            await self._sleep(wait)
            async with self._rotate_lock:
                if not self._flap_alert_pending:
                    return
                quiet_for = self._time_source() - self._flap_last_switch_at
                if quiet_for >= self._dwell_seconds:
                    self._fire_flap_alert()
                    return
            wait = self._FLAP_ALERT_RETRY_SECONDS

    def _ensure_probe_task(self) -> None:
        """Start the background primary-probe loop if one is warranted and not already
        running. Plain sync method -- called from inside _rotate_from's lock block, must
        not itself await. No-ops when recheck is disabled, there is only one link, we're
        already on primary, or a probe task already exists and hasn't finished."""
        if self._recheck_seconds <= 0 or len(self._chain) == 1 or self._index == 0:
            return
        if self._probe_task is not None and not self._probe_task.done():
            return
        try:
            self._probe_task = spawn(self._probe_loop())
        except RuntimeError:
            # spawn() needs a running event loop. The probe is purely an optimisation --
            # it must never break a rotation, so just skip it; the next rotation (or a
            # future call from within a running loop) will retry.
            logger.debug("_ensure_probe_task: no running loop, skipping background probe")

    def _probe_timeout(self) -> ClientTimeout:
        """Bound both connect AND total for the throwaway probe request -- a dead link
        must cost ~connect_timeout, not the default aiogram total. Extracted as its own
        method purely so it can be asserted on directly."""
        return ClientTimeout(
            total=max(self._connect_timeout * 2, self._connect_timeout + 5),
            connect=self._connect_timeout,
            sock_connect=self._connect_timeout,
        )

    def _probe_connector(self):
        """Build a throwaway connector for the primary link -- NEVER touches self._session
        or the live connector, so a probe can run concurrently with live requests on the
        backup link without any shared mutable state."""
        primary = self._chain[0]
        if primary is None or isinstance(primary, ApiLink):
            # The snapshot's SSLContext is shared/immutable and safe to reuse across
            # independent connector instances.
            return self._direct_connector_type(**dict(self._direct_connector_init))
        ctype, cinit = _prepare_connector(primary)
        return ctype(**cinit)

    def _probe_url(self) -> str:
        """Host to hit when probing the primary link: its own API base for an ApiLink,
        the session's default Bot API host (api.telegram.org) otherwise."""
        primary = self._chain[0]
        if isinstance(primary, ApiLink):
            return primary.base
        # TelegramAPIServer.base is "https://host/bot{token}/{method}"; the probe hits the
        # bare host (Telegram answers 404, which still proves the link carries traffic).
        base = self._default_api.base
        return base.split("/bot{token}", 1)[0]

    async def _probe_primary(self) -> bool:
        """Return True if the primary link carries traffic at all. Telegram answers a bare
        GET with 404 -- that still proves the link works, so ANY HTTP response counts as
        alive; only a raised exception (timeout, connection refused, DNS failure, ...)
        counts as dead."""
        try:
            async with aiohttp.ClientSession(connector=self._probe_connector()) as probe:
                async with probe.get(self._probe_url(), timeout=self._probe_timeout()):
                    return True
        except Exception:
            return False

    async def _probe_loop(self) -> None:
        """Background self-healing loop: while sitting on a backup link, periodically test
        whether the primary is back and, if so, return to it -- WITHOUT ever spending a
        live user request on a known-dead link (the prod 17.08 bug this whole design
        exists to fix).

        TRAP this design has to survive: do NOT cancel this task from close(). The base
        AiohttpSession.create_session() calls `await self.close()` whenever
        _should_reset_connector is set, and _apply() sets that flag on EVERY link swap --
        including the very rotation that starts this loop. A cancel inside close() would
        kill the probe on its own birth. The loop self-terminates once it returns to
        primary (the `while self._index != 0` condition), and process exit disposes of it
        otherwise -- no explicit cancellation is needed or safe here.
        """
        while self._index != 0:
            await self._sleep(self._recheck_seconds)
            if self._index == 0:
                # Another path (e.g. a manual apply, or a second loop instance somehow)
                # already returned to primary while we were sleeping.
                break
            try:
                alive = await self._probe_primary()
            except Exception:
                # Never let an unexpected exception silently kill the loop -- a failed
                # probe just means "not yet", try again next interval. (CancelledError is
                # a BaseException, not caught here, so the loop stays cancellable.)
                logger.debug("Proxy probe: primary check raised", exc_info=True)
                continue
            if not alive:
                continue
            async with self._rotate_lock:
                if self._index == 0:
                    break  # already returned via another path
                since = (
                    self._time_source() - self._switched_at
                    if self._switched_at is not None
                    else None
                )
                self._apply(0)
                self._switched_at = None
                # This is also a real switch for storm-guard purposes: a confirmed-alive
                # primary that immediately fails again right after we return to it should
                # get the same dwell grace period as any other rotation, instead of bouncing
                # straight back to the backup on the very next error.
                self._last_rotate_at = self._time_source()
                if since is not None:
                    logger.info(
                        "Proxy probe: primary %s is back, returning after %.0fs on backup",
                        mask_proxy_url(self._chain[0]),
                        since,
                    )
                else:
                    logger.info(
                        "Proxy probe: primary %s is back, returning",
                        mask_proxy_url(self._chain[0]),
                    )
            break

    def _request_timeout(self, timeout):
        """Wrap `timeout` in a ClientTimeout that bounds connection SETUP (connect/
        sock_connect) without changing the total response-wait budget. Pure-ish: reads
        only self._connect_timeout/self.timeout, no side effects.

        NOTE: never assign the result to self.timeout -- aiogram dispatcher.py:216 does
        `int(bot.session.timeout + polling_timeout)`, which requires self.timeout to stay
        numeric. This helper only ever returns a per-call value passed as make_request's
        `timeout=` kwarg.
        """
        if not self._connect_timeout or self._connect_timeout <= 0:
            return timeout
        total = self.timeout if timeout is None else timeout
        return ClientTimeout(
            total=total, connect=self._connect_timeout, sock_connect=self._connect_timeout
        )

    async def make_request(self, bot, method, timeout=None):
        effective = self._request_timeout(timeout)

        if len(self._chain) == 1:
            # Zero behavioural difference from the base class with a single link (parity
            # with pre-failover code when only PROXY_URL, or nothing, is set) other than
            # the connect bound applied above.
            return await super().make_request(bot, method, timeout=effective)

        first_error = None
        attempts = 0
        while True:
            current = self._index
            try:
                result = await super().make_request(bot, method, timeout=effective)
            except TelegramNetworkError as e:
                if first_error is None:
                    first_error = e
                attempts += 1
                if attempts >= len(self._chain):
                    # Full circle, nothing alive -- surface the ORIGINAL error so
                    # dp.start_polling's existing backoff/retry logic is unaffected.
                    raise first_error
                await self._rotate_from(current, e)
                continue
            # First of the two flap-stabilisation triggers (see _flap_alert_loop's
            # docstring): a live request succeeding on the current link is stronger
            # evidence than a bare timer that the storm is over. Cheap flag check before
            # ever touching the lock -- the overwhelming majority of requests never rotated.
            if self._flap_alert_pending:
                await self._stabilize_on_success()
            return result
