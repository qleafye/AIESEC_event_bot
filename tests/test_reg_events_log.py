"""Phase 15 Plan 01 (STAT-03, D-06): append-only registration-funnel event log.

Task 1 -- `reg_events` table + `record_reg_event()` + city-scoped `get_stats`/
`get_returning_count`. Task 2 -- the three fail-soft hooks in handlers/registration.py.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_export_stats_phase72.py.
"""
import asyncio
import inspect
import re

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import cities


def _use_tmp_db(tmp_path, name="test_reg_events.db"):
    config.DB_PATH = str(tmp_path / name)


# ── Task 1: reg_events table + record_reg_event ──────────────────────────────────────────

def test_init_db_creates_reg_events_table_and_index_idempotently(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.init_db())  # idempotent re-run must not raise

    async def _table_exists():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reg_events'"
            ) as cursor:
                return await cursor.fetchone() is not None

    async def _index_exists():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_reg_events_event_ts'"
            ) as cursor:
                return await cursor.fetchone() is not None

    assert asyncio.run(_table_exists())
    assert asyncio.run(_index_exists())


def test_record_reg_event_and_kinds_exported():
    assert db.REG_EVENT_KINDS == ("start", "form_started", "form_completed")
    assert callable(db.record_reg_event)


def test_record_reg_event_three_kinds_land_as_three_rows(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.record_reg_event(1, "start"))
    asyncio.run(db.record_reg_event(1, "form_started", event_city="spb", season="26/1"))
    asyncio.run(db.record_reg_event(1, "form_completed", event_city="spb", season="26/1"))

    async def _rows():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT telegram_id, event, event_city, season, ts FROM reg_events ORDER BY id"
            ) as cursor:
                return await cursor.fetchall()

    rows = asyncio.run(_rows())
    assert len(rows) == 3
    assert [r[1] for r in rows] == ["start", "form_started", "form_completed"]
    ts_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    for r in rows:
        assert ts_re.match(r[4])


def test_record_reg_event_repeat_is_append_not_dedup(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.record_reg_event(42, "start"))
    asyncio.run(db.record_reg_event(42, "start"))

    async def _count():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM reg_events WHERE telegram_id = ? AND event = 'start'", (42,)
            ) as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_count()) == 2


def test_record_reg_event_unknown_kind_still_writes_and_warns(tmp_path, caplog):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    with caplog.at_level("WARNING"):
        asyncio.run(db.record_reg_event(7, "totally_unexpected"))

    async def _count():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM reg_events WHERE telegram_id = ?", (7,)
            ) as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_count()) == 1
    assert any("unexpected event kind" in rec.message for rec in caplog.records)


# ── Task 1: get_stats / get_returning_count city_scope parity + scoping ─────────────────

def _seed_user(telegram_id, event_city, university="MGU", prev_season=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "university": university,
        "registration_date": f"2026-01-01 09:{telegram_id:02d}:00",
        "event_city": event_city,
        "prev_season": prev_season,
    }))


def test_get_stats_no_scope_byte_identical_result(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed_user(1, "spb")
    _seed_user(2, "msk")
    total_default, unis_default = asyncio.run(db.get_stats())
    total_explicit_none, unis_explicit_none = asyncio.run(db.get_stats(city_scope=None))
    assert total_default == total_explicit_none == 2
    assert unis_default == unis_explicit_none


def test_get_stats_city_scope_counts_only_that_city(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1, "spb")
    _seed_user(2, "spb")
    _seed_user(3, "msk")
    scope = cities.city_scope("spb")
    total, _unis = asyncio.run(db.get_stats(city_scope=scope))
    assert total == 2


def test_get_returning_count_no_scope_byte_identical(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    _seed_user(1, "spb", prev_season="25/2")
    _seed_user(2, "msk")
    default = asyncio.run(db.get_returning_count())
    explicit_none = asyncio.run(db.get_returning_count(city_scope=None))
    assert default == explicit_none == 1


def test_get_returning_count_city_scope_narrows(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1, "spb", prev_season="25/2")
    _seed_user(2, "msk", prev_season="25/2")
    scope = cities.city_scope("spb")
    assert asyncio.run(db.get_returning_count(city_scope=scope)) == 1


# ── Task 2: registration.py hooks (added once Task 2 lands) ─────────────────────────────

def test_registration_module_calls_record_reg_event_exactly_three_times():
    from handlers import registration as reg_mod
    from services import reg_finalize as reg_finalize_mod
    # Phase 21 (21-08): the "new" registration's form_completed call site moved out of
    # finalize_registration into services.reg_finalize.finalize_data (shared with the Mini
    # App outbox job) -- the other two (form_started/start, cmd_start-side) stayed put. Same
    # invariant (3 real call sites total), counted across both modules now.
    src = inspect.getsource(reg_mod) + inspect.getsource(reg_finalize_mod)
    # "await record_reg_event(" isolates real call sites -- a bare substring count would also
    # catch the top-of-file import and the fail-soft log messages that mention the name.
    assert src.count("await record_reg_event(") == 3


def test_form_completed_write_is_after_add_user_call():
    # Phase 21 (21-08): both call sites moved together into finalize_data's "new" branch.
    from services import reg_finalize as reg_finalize_mod
    src = inspect.getsource(reg_finalize_mod.finalize_data)
    add_user_idx = src.index("add_user(")
    form_completed_idx = src.index('"form_completed"')
    assert form_completed_idx > add_user_idx


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append(text)

    async def answer_photo(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append(caption)


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def test_start_hook_fires_exactly_once_row(tmp_path):
    from handlers import registration as reg_mod

    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    uid = 555001
    msg = _FakeMessage(uid, "tester")
    state = _new_state(uid)
    asyncio.run(reg_mod.cmd_start(msg, state, bot=object(), command=None))

    async def _count():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM reg_events WHERE telegram_id = ? AND event = 'start'",
                (uid,),
            ) as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_count()) == 1


def test_record_reg_event_failure_never_breaks_start(tmp_path, monkeypatch):
    from handlers import registration as reg_mod

    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _boom(*a, **k):
        raise RuntimeError("reg_events write failed")

    monkeypatch.setattr(reg_mod, "record_reg_event", _boom)

    uid = 555002
    msg = _FakeMessage(uid, "tester2")
    state = _new_state(uid)

    # Must not raise -- fail-soft hook swallows the monkeypatched exception.
    asyncio.run(reg_mod.cmd_start(msg, state, bot=object(), command=None))


# ── Quick task 260905-iyw: `start` gets a city (deep-link forward, backfill backward) ────

class _FakeCommand:
    def __init__(self, args=None):
        self.args = args


def test_start_hook_with_city_deep_link_carries_event_city(tmp_path):
    from handlers import registration as reg_mod

    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    uid = 555003
    msg = _FakeMessage(uid, "tester3")
    state = _new_state(uid)
    asyncio.run(reg_mod.cmd_start(msg, state, bot=object(), command=_FakeCommand("city_spb")))

    async def _city():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT event_city FROM reg_events WHERE telegram_id = ? AND event = 'start'",
                (uid,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    assert asyncio.run(_city()) == "spb"


def test_start_hook_without_deep_link_leaves_event_city_null(tmp_path):
    from handlers import registration as reg_mod

    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    uid = 555004
    msg = _FakeMessage(uid, "tester4")
    state = _new_state(uid)
    asyncio.run(reg_mod.cmd_start(msg, state, bot=object(), command=None))

    async def _city():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT event_city FROM reg_events WHERE telegram_id = ? AND event = 'start'",
                (uid,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    assert asyncio.run(_city()) is None


def test_backfill_reg_event_city_fills_only_own_null_start_row(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.record_reg_event(1, "start"))  # own NULL start -> gets filled
    asyncio.run(db.record_reg_event(2, "start"))  # other user's NULL start -> untouched
    asyncio.run(db.record_reg_event(1, "form_started", event_city="msk"))  # other event -> untouched
    asyncio.run(db.record_reg_event(1, "start", event_city="tyumen"))  # own already-filled start -> untouched

    asyncio.run(db.backfill_reg_event_city(1, "spb"))

    async def _rows():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT telegram_id, event, event_city FROM reg_events ORDER BY id"
            ) as cursor:
                return await cursor.fetchall()

    rows = asyncio.run(_rows())
    assert rows[0] == (1, "start", "spb")           # own NULL start -> filled
    assert rows[1] == (2, "start", None)            # other user's NULL start -> untouched
    assert rows[2] == (1, "form_started", "msk")    # other event -> untouched
    assert rows[3] == (1, "start", "tyumen")        # own already-filled start -> untouched


def test_backfill_reg_event_city_noop_on_falsy_city(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.record_reg_event(9, "start"))

    asyncio.run(db.backfill_reg_event_city(9, None))
    asyncio.run(db.backfill_reg_event_city(9, ""))

    async def _city():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT event_city FROM reg_events WHERE telegram_id = ? AND event = 'start'",
                (9,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    assert asyncio.run(_city()) is None


def test_form_started_backfills_city_onto_earlier_null_start_row(tmp_path):
    from handlers import registration as reg_mod

    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    uid = 555005
    msg = _FakeMessage(uid, "tester5")
    state = _new_state(uid)
    # No deep-link -> "start" row lands with event_city NULL.
    asyncio.run(reg_mod.cmd_start(msg, state, bot=object(), command=None))
    # Delegate reaches form_started with a city already resolved (e.g. picked in the flow).
    asyncio.run(reg_mod._start_registration_flow(msg, state, event_city="tyumen"))

    async def _start_city():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT event_city FROM reg_events WHERE telegram_id = ? AND event = 'start'",
                (uid,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    assert asyncio.run(_start_city()) == "tyumen"
