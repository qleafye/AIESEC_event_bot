"""Phase 07.1 Plan 02 (per-city sheet tab routing, CITY-02) tests.

pytest-asyncio is unavailable in this env — each async test drives the DB/registration
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file, same convention as
tests/test_cities_phase71.py / tests/test_sheets_phase5.py.
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum_city_sheets.db")


# ── Task 1: _sheet_kind / city_row_tab / city_incomplete_tab ────────────────────────────────

def test_sheet_kind_table():
    assert reg._sheet_kind(None) == "main"
    assert reg._sheet_kind("full") == "main"
    assert reg._sheet_kind("short") == "short"
    assert reg._sheet_kind("party_overnight") == "party"
    assert reg._sheet_kind("party_noovernight") == "party"


def test_city_row_tab_none_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg.city_row_tab("spb", None)

    assert asyncio.run(go()) is None


def test_city_row_tab_moscow_regression_all_tracks(tmp_path):
    """The Москва regression that matters most this phase: byte-identical to today (None ==
    legacy appender) across every track, once the module is on."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return (
            await reg.city_row_tab(None, None),
            await reg.city_row_tab("msk", None),
            await reg.city_row_tab("msk", "short"),
            await reg.city_row_tab("msk", "party_overnight"),
            await reg.city_row_tab("atlantis", None),  # unknown code -> Moscow fallback
        )

    results = asyncio.run(go())
    assert results == (None, None, None, None, None)


def test_city_row_tab_non_default_city_main_short_party(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return (
            await reg.city_row_tab("spb", None),
            await reg.city_row_tab("spb", "short"),
            await reg.city_row_tab("spb", "party_overnight"),
            await reg.city_row_tab("spb", "party_noovernight"),
            await reg.city_row_tab("tyumen", None),
            await reg.city_row_tab("tyumen", "short"),
        )

    results = asyncio.run(go())
    assert results == ("СПб", "СПб Акция", "СПб Party", "СПб Party", "Тюмень", "Тюмень Акция")


def test_city_row_tab_respects_tab_base_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_tab__spb", "Питер")
        return await reg.city_row_tab("spb", "short")

    assert asyncio.run(go()) == "Питер Акция"


def test_city_incomplete_tab_default_and_moscow(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        before_toggle = await reg.city_incomplete_tab(None)
        await db.set_setting("event_city_enabled", "on")
        return (
            before_toggle,
            await reg.city_incomplete_tab(None),
            await reg.city_incomplete_tab("msk"),
            await reg.city_incomplete_tab("spb"),
        )

    results = asyncio.run(go())
    assert results == ("Незавершённые", "Незавершённые", "Незавершённые", "СПб Незавершённые")


# ── Task 2: finalize_registration routing — city selects tab, track selects columns ─────────

class _FakeFromUser:
    def __init__(self, telegram_id, username="tester"):
        self.id = telegram_id
        self.username = username


class _FakeMessage:
    def __init__(self, telegram_id):
        self.from_user = _FakeFromUser(telegram_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _FakeState:
    def __init__(self, data):
        self._data = data

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        pass


async def _run_finalize_and_drain(message, state, bot):
    """finalize_registration schedules its sheet append via asyncio.create_task
    (fire-and-forget) — drain any tasks it spawned so the fake append callback has run
    before the test asserts on it."""
    await reg.finalize_registration(message, state, bot)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def _patch_appenders(monkeypatch):
    named_calls = []
    main_calls = []

    async def fake_named(tab_name, data):
        named_calls.append((tab_name, data))

    async def fake_main(data):
        main_calls.append(data)

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_named)
    monkeypatch.setattr(reg, "append_to_sheet", fake_main)
    return named_calls, main_calls


def test_finalize_registration_none_city_uses_legacy_main_appender(tmp_path, monkeypatch):
    """No-backfill case: a delegate whose FSM never set event_city -> add_user gets
    event_city is None, users.event_city stays NULL, but the write still routes to the
    Moscow (legacy) tab."""
    _use_tmp_db(tmp_path)
    named_calls, main_calls = _patch_appenders(monkeypatch)

    message = _FakeMessage(601)
    state = _FakeState({"full_name": "No City Delegate"})  # no "event_city" key at all

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("full_approval", "manual")
        await _run_finalize_and_drain(message, state, bot=None)
        return await db.get_user(601)

    user = asyncio.run(go())
    assert len(main_calls) == 1
    assert len(named_calls) == 0
    assert user["event_city"] is None


def test_finalize_registration_msk_city_uses_legacy_main_appender(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    named_calls, main_calls = _patch_appenders(monkeypatch)

    message = _FakeMessage(602)
    state = _FakeState({"full_name": "Moscow Delegate", "event_city": "msk"})

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("full_approval", "manual")
        await _run_finalize_and_drain(message, state, bot=None)

    asyncio.run(go())
    assert len(main_calls) == 1
    assert len(named_calls) == 0


def test_finalize_registration_spb_full_track_routes_to_named_tab(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    named_calls, main_calls = _patch_appenders(monkeypatch)

    message = _FakeMessage(603)
    state = _FakeState({"full_name": "SPb Delegate", "event_city": "spb"})

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("full_approval", "manual")
        await _run_finalize_and_drain(message, state, bot=None)

    asyncio.run(go())
    assert len(main_calls) == 0
    assert len(named_calls) == 1
    tab, row = named_calls[0]
    assert tab == "СПб"
    headers = asyncio.run(reg.get_sheet_schema())
    assert len(row) == len(headers)


def test_finalize_registration_spb_party_track_routes_to_named_party_tab(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    named_calls, main_calls = _patch_appenders(monkeypatch)

    message = _FakeMessage(604)
    state = _FakeState({
        "full_name": "SPb Party Guest",
        "event_city": "spb",
        "participant_type": "party_overnight",
    })

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("party_approval", "manual")
        await _run_finalize_and_drain(message, state, bot=None)

    asyncio.run(go())
    assert len(main_calls) == 0
    assert len(named_calls) == 1
    tab, row = named_calls[0]
    assert tab == "СПб Party"
    party_headers = asyncio.run(reg.party_sheet_headers())
    assert len(row) == len(party_headers)
    assert dict(zip(party_headers, row)).get("Трек") == "С ночёвкой"


def test_finalize_registration_spb_short_track_routes_to_named_short_tab(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    named_calls, main_calls = _patch_appenders(monkeypatch)

    message = _FakeMessage(605)
    state = _FakeState({
        "full_name": "SPb Promo Delegate",
        "event_city": "spb",
        "participant_type": "short",
    })

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("short_approval", "manual")
        await _run_finalize_and_drain(message, state, bot=None)

    asyncio.run(go())
    assert len(main_calls) == 0
    assert len(named_calls) == 1
    tab, row = named_calls[0]
    assert tab == "СПб Акция"
    short_headers = asyncio.run(reg.short_sheet_headers())
    assert len(row) == len(short_headers)
