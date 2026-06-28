"""Phase 3 (VERIF-01/02): in-RAM pre-selection allowlist.

The allowlist of selected Telegram usernames lives in a separate Google-Sheet tab
(D-09). `/start` never calls gspread — it checks this RAM set (D-11/D-13). The set is
refreshed at startup, on a mandatory interval, and on an admin trigger.
"""
import asyncio
import logging

from database.db import get_setting

logger = logging.getLogger(__name__)

DEFAULT_TAB = "Отобранные"

# Module-global cache. Rebuilt wholesale by refresh_allowlist (never mutated in place).
_allowlist: set[str] = set()


def _normalize(username: str) -> str:
    """Strip whitespace, drop a leading @, lowercase (D-10) — applied to both sides."""
    return username.strip().lstrip("@").lower()


def _parse_manual_ids(raw: str | None) -> set[int]:
    """Parse a CSV of telegram_ids (D-12); non-int tokens are skipped."""
    out: set[int] = set()
    if not raw:
        return out
    for token in raw.split(","):
        token = token.strip()
        try:
            out.add(int(token))
        except (TypeError, ValueError):
            continue
    return out


def is_allowed(username: str | None) -> bool:
    """True iff the normalized username is in the cached allowlist."""
    return bool(username) and _normalize(username) in _allowlist


def allowlist_size() -> int:
    return len(_allowlist)


async def refresh_allowlist():
    """Reload the RAM set from the allowlist tab. Fail-soft: a missing tab / quota
    error logs a warning and leaves the previous set intact, never crashes the caller."""
    global _allowlist
    try:
        from services.sheets import _get_allowlist_rows_sync  # local import avoids circular
        tab = await get_setting("preselect_tab") or DEFAULT_TAB
        rows = await asyncio.to_thread(_get_allowlist_rows_sync, tab)
        _allowlist = {_normalize(v) for v in rows[1:] if v and v.strip()}  # skip header
        logger.info(f"Allowlist refreshed: {len(_allowlist)} usernames from tab {tab!r}")
    except Exception as e:
        logger.warning(f"Allowlist refresh failed (keeping previous {len(_allowlist)} entries): {e}")
