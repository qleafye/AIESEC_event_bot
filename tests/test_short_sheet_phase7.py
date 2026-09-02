"""Phase 7 Plan 2 (Configurable short registration form — sheet tab) tests.

pytest-asyncio is unavailable in this env, so async helpers are driven via asyncio.run()
and config.DB_PATH/config.GOOGLE_SHEET_ID are pointed at test-safe values — same convention
as tests/test_sheets_phase5.py (this plan's direct copy-target for headers/row-by-track and
_spawn-interception style) and tests/test_short_track_phase7.py (fake message/state).
"""
import asyncio
import inspect

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


# ── Group 1: width by the short gate (isolation from the global reg_q_* set) ───────────────

def test_short_sheet_headers_width_follows_short_gate_only(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        # Global toggles deliberately set OPPOSITE of the short-set below — proves isolation:
        # if short_sheet_headers ever fell back to the global gate, phone/city would vanish
        # and alumni_status would appear instead.
        await db.set_setting("reg_q_phone", "off")
        await db.set_setting("reg_q_city", "off")
        await db.set_setting("reg_q_alumni_status", "on")
        await db.set_setting("reg_q_phone__short", "on")
        await db.set_setting("reg_q_city__short", "on")
        return await reg.short_sheet_headers()

    headers = asyncio.run(go())
    assert headers == [
        "ID Telegram", "Username", "Дата регистрации", "Статус", "ФИО", "Телефон", "Город",
    ]


# ── Group 2: zero __short keys -> only the 5 system columns ────────────────────────────────

def test_short_sheet_headers_zero_keys_is_system_only(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg.short_sheet_headers()

    headers = asyncio.run(go())
    assert headers == ["ID Telegram", "Username", "Дата регистрации", "Статус", "ФИО"]


# ── Group 3: formula-injection neutralization (T-07-04) ─────────────────────────────────────

def test_short_sheet_row_neutralizes_formula_injection(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        headers = await reg.short_sheet_headers()
        data = {
            "telegram_id": 1, "username": "@x", "registration_date": "2026-08-07 10:00:00",
            "status": "pending", "full_name": '=HYPERLINK("http://evil","x")',
        }
        row = await reg.short_sheet_row(data)
        return dict(zip(headers, row))

    values = asyncio.run(go())
    assert values["ФИО"].startswith("'")


# ── Group 4: exclusivity of _sheet_dispatch (SHORT-04 regression + structural guard) ────────

def test_sheet_dispatch_routes_short_to_short_pair():
    row_fn, append_fn = reg._sheet_dispatch("short")
    assert row_fn is reg.short_sheet_row
    assert append_fn is reg.append_to_short_sheet


def test_sheet_dispatch_routes_party_tracks_to_party_pair():
    for pt in ("party_overnight", "party_noovernight"):
        row_fn, append_fn = reg._sheet_dispatch(pt)
        assert row_fn is reg.party_sheet_row
        assert append_fn is reg.append_to_party_sheet


def test_sheet_dispatch_routes_full_none_and_unknown_to_main_pair():
    for pt in ("full", None, "bogus"):
        row_fn, append_fn = reg._sheet_dispatch(pt)
        assert row_fn is reg.active_sheet_row
        assert append_fn is reg.append_to_sheet


def test_sheet_dispatch_exactly_one_tab_per_input():
    """Property: collect the appenders across every known input — exactly three distinct
    functions come back, no input returns None, and no input returns a tuple of length != 2."""
    inputs = ["short", "party_overnight", "party_noovernight", "full", None, "bogus"]
    results = [reg._sheet_dispatch(pt) for pt in inputs]
    for pair in results:
        assert pair is not None
        assert len(pair) == 2
        assert all(fn is not None for fn in pair)
    appenders = {append_fn for _row_fn, append_fn in results}
    assert appenders == {reg.append_to_short_sheet, reg.append_to_party_sheet, reg.append_to_sheet}


def test_sheet_dispatch_short_pair_excludes_main_sheet_appender():
    """Regression guard for SHORT-04: the short track must never also write to the main tab."""
    _row_fn, append_fn = reg._sheet_dispatch("short")
    assert append_fn is not reg.append_to_sheet
    assert append_fn is not reg.append_to_party_sheet


def test_finalize_registration_dispatch_is_the_only_branch():
    """Structural guard: the fork lives in _sheet_dispatch, not inlined by name anywhere in
    the finalize path. No direct append_to_*( calls may remain.

    Phase 21 (21-08): the append itself moved out of finalize_registration into the shared
    services.reg_finalize.post_finalize (bot-direct call AND the Mini App outbox job now
    share this one Sheets path) -- the guard now looks there instead."""
    from services import reg_finalize as reg_finalize_mod

    reg_src = inspect.getsource(reg.finalize_registration)
    assert "_sheet_dispatch" not in reg_src
    assert "append_to_sheet(" not in reg_src
    assert "append_to_party_sheet(" not in reg_src
    assert "append_to_short_sheet(" not in reg_src

    finalize_src = inspect.getsource(reg_finalize_mod.post_finalize)
    assert "_sheet_dispatch" in finalize_src
    assert "append_to_sheet(" not in finalize_src
    assert "append_to_party_sheet(" not in finalize_src
    assert "append_to_short_sheet(" not in finalize_src


# ── Group 5: tab name resolution (default vs configured) ────────────────────────────────────

def test_append_to_short_sheet_uses_default_tab_when_unset(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    captured = {}

    async def fake_append(tab_name, data):
        captured["tab"] = tab_name

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_append)

    async def go():
        await db.init_db()
        await reg.append_to_short_sheet([1, 2, 3])

    asyncio.run(go())
    assert captured["tab"] == reg.SHORT_SHEET_TAB_DEFAULT == "Краткая"


def test_append_to_short_sheet_uses_configured_tab(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    captured = {}

    async def fake_append(tab_name, data):
        captured["tab"] = tab_name

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_append)

    async def go():
        await db.init_db()
        await db.set_setting("short_sheet_tab", "Акция")
        await reg.append_to_short_sheet([1, 2, 3])

    asyncio.run(go())
    assert captured["tab"] == "Акция"


# ── Group 6: startup materialization gate (T-07-06) ──────────────────────────────────────────

def test_maybe_ensure_short_sheet_header_noop_when_mode_full(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    import main

    called = {"hit": False}

    async def fake_ensure(tab_name, headers):
        called["hit"] = True

    monkeypatch.setattr(main.sheets_service, "ensure_named_sheet_header", fake_ensure)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "full")
        await main._maybe_ensure_short_sheet_header()

    asyncio.run(go())
    assert called["hit"] is False


def test_maybe_ensure_short_sheet_header_calls_ensure_once_when_mode_short(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    import main

    calls = []

    async def fake_ensure(tab_name, headers):
        calls.append((tab_name, headers))

    monkeypatch.setattr(main.sheets_service, "ensure_named_sheet_header", fake_ensure)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await main._maybe_ensure_short_sheet_header()

    asyncio.run(go())
    assert len(calls) == 1
    tab_name, headers = calls[0]
    assert tab_name == main.SHORT_SHEET_TAB_DEFAULT == "Краткая"
    assert isinstance(headers, list) and "ФИО" in headers


# ── Group 7: main sheet unaffected by __short toggles ────────────────────────────────────────

def test_active_sheet_headers_unchanged_by_short_toggles(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        before = await reg.active_sheet_headers()
        await db.set_setting("reg_q_phone__short", "on")
        await db.set_setting("reg_q_city__short", "on")
        await db.set_setting("reg_q_alumni_status__short", "off")
        after = await reg.active_sheet_headers()
        return before, after

    before, after = asyncio.run(go())
    assert before == after
