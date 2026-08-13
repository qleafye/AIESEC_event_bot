"""Phase 07.1 Plan 04 (CITY-04) tests: admin «🏙 Города мероприятия» screen, per-city
«Незавершённые» batching (manual export + 2h auto-sync parity), and doc-string-safety
checks tying ADMIN_GUIDE.md to the implemented tab names/tokens.

pytest-asyncio is unavailable in this env — every async helper is driven via
asyncio.run() and config.DB_PATH points at a tmp_path file (same idiom as
tests/test_admin_phase5.py).
"""
import asyncio

import aiosqlite

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import registration as reg_mod
from handlers.admin_caps import required_capability
from cities import CITIES


ADMIN_ID = 900101
NON_ADMIN_ID = 900102


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_admin71.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


async def _settings_row_count() -> int:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute("SELECT COUNT(*) FROM bot_settings") as cur:
            row = await cur.fetchone()
            return row[0]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None):
        self.answers_sent.append(text)


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


CITY_CODES = [c["code"] for c in CITIES]  # msk/spb/tyumen from .env defaults


# ── Task 1: «🏙 Города мероприятия» admin screen ──────────────────────────────

def test_build_admin_keyboard_admin_cities_is_last_row_indices_unchanged(tmp_path):
    # 08-05 (D-15): build_admin_keyboard is now async and capability-filtered — for a bootstrap
    # admin (ALL_CAPABILITIES), the row set/order is unchanged from the pre-08-05 plain builder.
    # QUICK T-08-33 (2026-08-13) inserted "admin_stuck_questions" right after "admin_receipts"
    # -- the first-block length grew from 13 to 14. 09-02 (GAME-01) appended "admin_game_tasks"
    # after "admin_cities". 09-04 (GAME-02/03) appended "admin_game_review" after that. 09-05
    # (sheet export) appended "admin_game_sync_sheet" after that -- admin_cities is now
    # fourth-to-last, admin_game_tasks third-to-last, admin_game_review second-to-last,
    # admin_game_sync_sheet last.
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_admin_keyboard(ADMIN_ID))
    rows = kb.inline_keyboard
    assert rows[-1][0].callback_data == "admin_game_sync_sheet"
    assert rows[-2][0].callback_data == "admin_game_review"
    assert rows[-3][0].callback_data == "admin_game_tasks"
    assert rows[-4][0].callback_data == "admin_cities"
    expected_first_14 = [
        "admin_stats", "admin_monthly_stats", "admin_source_stats", "admin_export_csv",
        "admin_export_incomplete", "admin_applications", "admin_receipts",
        "admin_stuck_questions", "admin_broadcast",
        "admin_sync_sheet", "admin_rebuild_sheet", "admin_dedupe_sheet", "admin_settings",
        "admin_settings_guide",
    ]
    actual_first_14 = [rows[i][0].callback_data for i in range(14)]
    assert actual_first_14 == expected_first_14


def test_build_cities_keyboard_contains_toggle_and_per_city_buttons(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_cities_keyboard())
    flat = _flat_callback_data(kb)
    assert "toggle_event_city_enabled" in flat
    for code in CITY_CODES:
        assert f"city_toggle:{code}" in flat
        assert f"settings_edit:city_label__{code}" in flat
    assert len(CITY_CODES) == 3


def test_admin_cities_screen_is_capability_guarded(tmp_path):
    # Phase 8 / D-01: the old per-handler `config.ADMIN_IDS` check (and the direct-call test
    # that exercised it) is gone (08-04, one-shot migration, D-03) -- CapabilityMiddleware is
    # now the ONLY enforcement point, and it only wraps events dispatched through the real
    # router, not direct handler calls. The structural guarantee survives with a new carrier:
    # the handler stays registered, and its callback_data resolves to a real capability.
    _admin_ready(tmp_path)
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "show_admin_cities" in names
    assert required_capability(callback_data="admin_cities") == "settings"


def test_toggle_event_city_enabled_is_capability_guarded(tmp_path):
    # Phase 8 / D-01: see test_admin_cities_screen_is_capability_guarded above.
    _admin_ready(tmp_path)
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "toggle_event_city_enabled" in names
    assert required_capability(callback_data="toggle_event_city_enabled") == "settings"


def test_city_toggle_is_capability_guarded(tmp_path):
    # Phase 8 / D-01: see test_admin_cities_screen_is_capability_guarded above.
    _admin_ready(tmp_path)
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "city_toggle" in names
    assert required_capability(callback_data="city_toggle:spb") == "settings"


def test_toggle_event_city_enabled_flips_off_to_on_to_off(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(admin_mod.toggle_event_city_enabled(FakeCallback("toggle_event_city_enabled")))
    assert asyncio.run(db.get_setting("event_city_enabled")) == "on"
    asyncio.run(admin_mod.toggle_event_city_enabled(FakeCallback("toggle_event_city_enabled")))
    assert asyncio.run(db.get_setting("event_city_enabled")) == "off"


def test_city_toggle_spb_flips_default_on_to_off_to_on(tmp_path):
    _admin_ready(tmp_path)
    # default (no bot_settings row) resolves to "on" — cities.is_city_enabled
    asyncio.run(admin_mod.city_toggle(FakeCallback("city_toggle:spb")))
    assert asyncio.run(db.get_setting("city_enabled__spb")) == "off"
    asyncio.run(admin_mod.city_toggle(FakeCallback("city_toggle:spb")))
    assert asyncio.run(db.get_setting("city_enabled__spb")) == "on"


def test_city_toggle_unknown_code_rejected_no_write(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("city_toggle:atlantis")
    asyncio.run(admin_mod.city_toggle(cb))
    assert cb.answers[-1][1] is True  # show_alert
    assert asyncio.run(_settings_row_count()) == 0


def test_render_cities_text_has_deep_link_and_label_escaped(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_mod.render_cities_text())
    assert "?start=city_spb" in text
    spb_label = asyncio.run(db.get_setting("city_label__spb"))
    assert spb_label is None  # unset -> falls back to .env label, not asserted verbatim here

    asyncio.run(db.set_setting("city_label__spb", "<b>x</b>"))
    text2 = asyncio.run(admin_mod.render_cities_text())
    assert "<b>x</b>" not in text2
    assert "&lt;b&gt;x&lt;/b&gt;" in text2


# ── Task 2: «Незавершённые» split per city (manual export + 2h auto-sync parity) ──────────

def test_get_incomplete_rows_with_city_returns_six_tuple(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def go():
        await db.mark_reg_started(1, "vasya", event_city="spb")
        rows = await db.get_incomplete_rows_with_city()
        assert len(rows) == 1
        row = rows[0]
        assert len(row) == 6
        assert row[0] == 1
        assert row[1] == "vasya"
        assert row[5] == "spb"

    asyncio.run(go())


def test_get_incomplete_rows_unaffected_still_five_tuple(tmp_path):
    """get_incomplete_rows() itself must stay untouched — existing tests rely on the 5-tuple."""
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def go():
        await db.mark_reg_started(1, "vasya", event_city="spb")
        rows = await db.get_incomplete_rows()
        assert len(rows[0]) == 5

    asyncio.run(go())


def test_incomplete_city_batches_module_off_collapses_to_single_default_tab(tmp_path):
    """Regression: with the module off, three dropouts from three different cities must still
    land in ONE batch on the plain «Незавершённые» tab — today's behavior, byte for byte."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "off")
        await db.mark_reg_started(1, "a", event_city="msk")
        await db.mark_reg_started(2, "b", event_city="spb")
        await db.mark_reg_started(3, "c", event_city="tyumen")

        batches = await reg_mod.incomplete_city_batches()
        assert len(batches) == 1
        tab, _headers, rows = batches[0]
        assert tab == "Незавершённые"
        assert len(rows) == 3

    asyncio.run(go())


def test_incomplete_city_batches_module_on_splits_msk_and_spb(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.mark_reg_started(1, "a", event_city="msk")
        await db.mark_reg_started(2, "b", event_city="spb")

        batches = await reg_mod.incomplete_city_batches()
        by_tab = {tab: len(rows) for tab, _h, rows in batches}
        assert by_tab == {"Незавершённые": 1, "СПб Незавершённые": 1}

    asyncio.run(go())


def test_incomplete_city_batches_default_tab_present_even_when_empty(tmp_path):
    """Only spb dropouts exist -> the default («Незавершённые») tab must still be in the
    result WITH AN EMPTY row list, so the full clear+rewrite keeps wiping Moscow dropouts
    that have since registered/been cleared. Spb's tab is only included because it has rows."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.mark_reg_started(2, "b", event_city="spb")

        batches = await reg_mod.incomplete_city_batches()
        by_tab = {tab: len(rows) for tab, _h, rows in batches}
        assert len(batches) == 2
        assert by_tab["Незавершённые"] == 0
        assert by_tab["СПб Незавершённые"] == 1

    asyncio.run(go())


def test_incomplete_city_batches_null_event_city_goes_to_default_tab(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.mark_reg_started(1, "a")  # no event_city -> NULL in reg_started

        batches = await reg_mod.incomplete_city_batches()
        assert len(batches) == 1
        tab, _headers, rows = batches[0]
        assert tab == "Незавершённые"
        assert len(rows) == 1

    asyncio.run(go())


def test_incomplete_city_batches_headers_shared_across_batches(tmp_path):
    """headers computed exactly once per call (Google Sheets quota) — every batch shares it."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.mark_reg_started(1, "a", event_city="msk")
        await db.mark_reg_started(2, "b", event_city="spb")

        batches = await reg_mod.incomplete_city_batches()
        assert len(batches) == 2
        headers_list = [h for _t, h, _r in batches]
        assert headers_list[0] is headers_list[1]

    asyncio.run(go())


def test_incomplete_city_batches_empty_non_default_city_tab_never_materialized(tmp_path):
    """No rows at all anywhere -> only the default tab, never an empty spb/tyumen tab."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")

        batches = await reg_mod.incomplete_city_batches()
        assert len(batches) == 1
        assert batches[0][0] == "Незавершённые"

    asyncio.run(go())


def test_export_incomplete_and_scheduler_sync_produce_same_batches(tmp_path, monkeypatch):
    """WR-01 parity, extended to per-city tabs (CITY-04): manual export and the 2h auto-sync
    job must write the exact same set of (tab, row_count) pairs from the same DB state."""
    _admin_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.mark_reg_started(1, "a", event_city="msk")
        await db.mark_reg_started(2, "b", event_city="spb")
        await db.mark_reg_started(3, "c", event_city="tyumen")
        await db.mark_reg_started(4, "d", event_city="spb")

        admin_calls = []

        async def _fake_admin_sync(title, headers, rows):
            admin_calls.append((title, len(rows)))
            return len(rows)

        monkeypatch.setattr(admin_mod, "sync_named_worksheet", _fake_admin_sync)
        await admin_mod.export_incomplete(FakeCallback("admin_export_incomplete"))

        import services.scheduler as scheduler_mod
        import services.sheets as sheets_mod
        scheduler_calls = []

        async def _fake_sheets_sync(title, headers, rows):
            scheduler_calls.append((title, len(rows)))
            return len(rows)

        monkeypatch.setattr(sheets_mod, "sync_named_worksheet", _fake_sheets_sync)
        await scheduler_mod.sync_incomplete_sheet_job()

        assert set(admin_calls) == set(scheduler_calls)
        assert len(admin_calls) == len(scheduler_calls) == 3  # msk, spb, tyumen

    asyncio.run(go())


def test_export_incomplete_is_capability_guarded(tmp_path):
    # Phase 8 / D-01: see test_admin_cities_screen_is_capability_guarded above.
    _admin_ready(tmp_path)
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "export_incomplete" in names
    assert required_capability(callback_data="admin_export_incomplete") == "stats"


# ── Task 3: documentation must stay in sync with implemented tab names/tokens ─────────────

def test_admin_guide_documents_city_deep_links_and_spb_incomplete_tab():
    with open("ADMIN_GUIDE.md", "r", encoding="utf-8") as f:
        text = f.read()
    assert "?start=city_spb" in text
    assert "СПб Незавершённые" in text
