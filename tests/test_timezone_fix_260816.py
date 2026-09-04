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
import asyncio
import glob
import importlib.metadata
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from database.db import add_user, set_setting

import services.scheduler as sched_mod
from services.scheduler import MOSCOW_TZ, _now_moscow_naive

ADMIN_ID = 260816001


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_timezone_fix_260816.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int = ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Same shape as tests/test_gamification_admin_phase9.py's FakeMessage."""

    def __init__(self, text=None):
        self.text = text
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)


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
    """The quoted "Europe/Moscow" literal must live in exactly one place (services/timeutil.py)
    so the scheduler pin and the admin-input validations physically cannot read a different
    timezone — that divergence was the root cause of TZFIX-260816.

    Quick 260904-kk6 (Q1): the literal moved scheduler.py -> timeutil.py — `services/questions.py`
    needs MOSCOW_TZ too but cannot import `services.scheduler` (aiogram + APScheduler), and
    `services/questions.py` is imported from `miniapp/routers/questions.py`, which must stay
    aiogram-free (D-01). `services/scheduler.py` now re-exports MOSCOW_TZ from timeutil.py.
    """
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
    assert hits[0][0].replace("\\", "/") == "services/timeutil.py"


# ── Task 2: three fixed points on Moscow wall-clock, fourth deliberately untouched ──────

# Oracle scenario shared by all three fixed points: "now" = 01.07.2026 14:00 MSK
# (= 11:00 on the container's UTC clock). A candidate value of 01.07.2026 12:30 has
# ALREADY PASSED by Moscow wall-clock, but looks like it's in the future by UTC — this is
# exactly the 3-hour window the bug lived in.

def test_broadcast_schedule_rejects_time_inside_utc_msk_window(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin as admin_mod
    from handlers import admin_broadcasts  # Phase 13 (13-05): broadcast handlers moved here
    from handlers.states import Broadcast

    state = _new_state()
    asyncio.run(state.set_state(Broadcast.schedule_when))

    orig = admin_broadcasts._now_moscow_naive
    admin_broadcasts._now_moscow_naive = lambda: datetime(2026, 7, 1, 14, 0)
    try:
        past = FakeMessage(text="01.07.2026 12:30")
        asyncio.run(admin_broadcasts.broadcast_schedule_when(past, state))
        assert asyncio.run(state.get_state()) == Broadcast.schedule_when, "must stay on schedule_when"
        assert "прошло" in past.answers_sent[-1].lower()
        assert asyncio.run(state.get_data()).get("schedule_dt") is None

        future = FakeMessage(text="01.07.2026 15:00")
        asyncio.run(admin_broadcasts.broadcast_schedule_when(future, state))
        assert asyncio.run(state.get_state()) == Broadcast.schedule_message
    finally:
        admin_broadcasts._now_moscow_naive = orig


def test_game_task_deadline_rejects_time_inside_utc_msk_window(tmp_path):
    _db_ready(tmp_path)
    from handlers import admin_gamification  # Phase 13 (13-04): game_task_deadline_step moved here
    from handlers.states import GameTaskCreate

    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.deadline))

    orig = admin_gamification._now_moscow_naive
    admin_gamification._now_moscow_naive = lambda: datetime(2026, 7, 1, 14, 0)
    try:
        past = FakeMessage(text="01.07.2026 12:30")
        asyncio.run(admin_gamification.game_task_deadline_step(past, state))
        assert asyncio.run(state.get_state()) == GameTaskCreate.deadline, "must stay on deadline step"
        assert "прошло" in past.answers_sent[-1].lower()

        future = FakeMessage(text="01.07.2026 15:00")
        asyncio.run(admin_gamification.game_task_deadline_step(future, state))
        assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    finally:
        admin_gamification._now_moscow_naive = orig


def test_payment_sweep_uses_moscow_wall_clock(tmp_path):
    _db_ready(tmp_path)
    telegram_id = 260816002
    asyncio.run(add_user({"telegram_id": telegram_id, "full_name": "TZ Fix", "registration_date": "x"}))
    asyncio.run(set_setting("payment_deadline", "01.07.2026 12:30"))

    class _FakeBot:
        async def send_message(self, *args, **kwargs):
            pass

    async def _set_not_paid():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "UPDATE users SET payment_status='not_paid', payment_option='A' WHERE telegram_id=?",
                (telegram_id,),
            )
            await conn.commit()

    async def _read_status():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT payment_status FROM users WHERE telegram_id=?", (telegram_id,)
            )
            row = await cur.fetchone()
            return row[0]

    sched_mod._bot = _FakeBot()
    orig = sched_mod._now_moscow_naive
    try:
        # "Now" (Moscow) is BEFORE the deadline -> stays not_paid.
        asyncio.run(_set_not_paid())
        sched_mod._now_moscow_naive = lambda: datetime(2026, 7, 1, 12, 0)
        asyncio.run(sched_mod.sweep_payment_overdue())
        assert asyncio.run(_read_status()) == "not_paid"

        # "Now" (Moscow) is AFTER the deadline -> flips to overdue.
        asyncio.run(_set_not_paid())
        sched_mod._now_moscow_naive = lambda: datetime(2026, 7, 1, 14, 0)
        asyncio.run(sched_mod.sweep_payment_overdue())
        assert asyncio.run(_read_status()) == "overdue"
    finally:
        sched_mod._now_moscow_naive = orig
        sched_mod._bot = None


def test_nudge_cutoff_stays_on_container_clock():
    """scheduler.py:413 сознательно на часах контейнера, ТЗ TZFIX-260816 — приведение к
    Москве по симметрии с точками 345/admin:2312/admin:4711 было бы регрессом (129 уже
    записанных started_at «постареют» на 3 часа)."""
    orig_now_moscow = sched_mod._now_moscow_naive
    orig_cutoff = sched_mod._nudge_cutoff
    orig_get_setting = sched_mod.get_setting
    orig_get_candidates = db.get_nudge_candidates

    cutoff_doc = inspect.getdoc(orig_cutoff)
    assert "container clock" in cutoff_doc, (
        "docstring anchor missing — future editor could 'fix' scheduler.py:413 by symmetry"
    )

    captured_now = []

    def fake_cutoff(now, minutes):
        captured_now.append(now)
        return orig_cutoff(now, minutes)

    async def fake_get_setting(key):
        if key == "nudge_enabled":
            return "on"
        if key == "nudge_after_minutes":
            return "120"
        return None

    async def fake_get_candidates(_cutoff):
        return []  # no candidates -> job returns before needing a bot

    sched_mod._now_moscow_naive = lambda: datetime(2000, 1, 1)
    sched_mod._nudge_cutoff = fake_cutoff
    sched_mod.get_setting = fake_get_setting
    db.get_nudge_candidates = fake_get_candidates
    try:
        asyncio.run(sched_mod.nudge_incomplete_registrations())
        assert len(captured_now) == 1
        now = captured_now[0]
        assert abs((now - datetime.now()).total_seconds()) <= 5, (
            "cutoff must be fed the container clock, not the (patched, absurd) Moscow helper"
        )
        assert now != datetime(2000, 1, 1)
    finally:
        sched_mod._now_moscow_naive = orig_now_moscow
        sched_mod._nudge_cutoff = orig_cutoff
        sched_mod.get_setting = orig_get_setting
        db.get_nudge_candidates = orig_get_candidates
