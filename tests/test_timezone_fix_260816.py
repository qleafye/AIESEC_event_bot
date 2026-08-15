"""TZFIX-260816: surgical timezone fix.

Bug: the container (python:3.11-slim, no ENV TZ) runs on UTC while the scheduler is pinned
to Europe/Moscow (services/scheduler.py:98). In the 3-hour window between the two, admin-input
validations comparing against a bare `datetime.now()` (container/UTC clock) let a PAST Moscow
wall-clock time through as "future" — and `misfire_grace_time=86400` then fired the stale
broadcast job immediately to the whole audience instead of rejecting the input.

Fixed by introducing a single Moscow-wall-clock helper (`_now_moscow_naive`) sourced from the
SAME `MOSCOW_TZ` constant the scheduler pin already uses, and switching the three admin-input
comparison points onto it. `scheduler.py:413` (`_nudge_cutoff` call site) is deliberately left
on the container clock — it compares against a value the bot itself stamped via `datetime.now()`,
not against admin input, so "fixing" it by symmetry would be a regression.

See .planning/TZFIX-260816.md for the full brief and decision log.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run() and
config.DB_PATH points at a tmp_path file, the established convention across tests/ (see
tests/test_gamification_admin_phase9.py, tests/test_settings_consumers_phase6.py).
"""
import glob
import importlib.metadata
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import services.scheduler as sched_mod
from services.scheduler import MOSCOW_TZ, _now_moscow_naive


# ── Task 1: helper, single source of the timezone literal, explicit tzdata dep ──────────

def test_now_moscow_naive_is_naive_moscow_wall_clock():
    value = _now_moscow_naive()
    assert value.tzinfo is None

    oracle = datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    assert abs((value - oracle).total_seconds()) <= 5

    # The property the whole fix hinges on: the 3-hour window between the container's UTC
    # clock and Moscow wall-clock, independent of the machine running the test suite.
    utc_now = datetime.utcnow()
    diff_minutes = round((_now_moscow_naive() - utc_now).total_seconds() / 60)
    assert diff_minutes == 180, (
        f"expected exactly the 3h MSK-UTC offset window this bug lived in, got {diff_minutes}min"
    )

    assert MOSCOW_TZ.utcoffset(datetime(2026, 7, 1, 12, 0)) == timedelta(hours=3)


def test_moscow_tz_resolves_and_tzdata_declared():
    # Resolves without relying on the system's tzdb (python:3.11-slim may not ship one).
    assert ZoneInfo("Europe/Moscow") is not None
    assert importlib.metadata.version("tzdata")

    req = Path("requirements.txt").read_text(encoding="utf-8")
    assert any(
        line.strip().startswith("tzdata") for line in req.splitlines()
    ), "tzdata must be declared in requirements.txt — do not rely on system tzdb in prod"


def test_moscow_literal_declared_exactly_once():
    """The quoted "Europe/Moscow" literal must live in exactly one place (services/scheduler.py)
    so the scheduler pin and the admin-input validations physically cannot read a different
    timezone — that divergence was the root cause of TZFIX-260816."""
    hits = []
    for pattern in ("services/*.py", "handlers/*.py"):
        for path in glob.glob(pattern):
            text = Path(path).read_text(encoding="utf-8")
            count = text.count('"Europe/Moscow"')
            if count:
                hits.append((path, count))

    total = sum(c for _, c in hits)
    assert total == 1, (
        f"пояс задаётся в одном месте, MOSCOW_TZ — нашли {total} вхождений в {hits}"
    )
    assert hits[0][0].replace("\\", "/") == "services/scheduler.py"
