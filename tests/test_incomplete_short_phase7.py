"""Phase 7 Plan 4 (SHORT-06): «Незавершённые» tab must not lose promo-track answers.

pytest-asyncio is unavailable in this env, so async helpers are driven via asyncio.run() and
config.DB_PATH is pointed at a tmp_path file — same convention as tests/test_partial_fields_k4y.py
and tests/test_short_track_phase7.py.
"""
import asyncio
import inspect

from config import config
from database import db
from handlers import registration as reg


def _use_tmp(tmp_path):
    config.DB_PATH = str(tmp_path / "incomplete_short.db")


# ── Group 1: no regression when short is untouched ───────────────────────────────────────

def test_full_mode_no_short_incomplete_matches_pre_phase_formula(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "full")
        # a pile of __short-only toggles is turned on, but nothing gates on them without
        # either the mode being "short" or a live short reg_started row
        await db.set_setting("reg_q_city__short", "on")
        await db.set_setting("reg_q_phone__short", "on")
        await db.set_setting("reg_q_vk__short", "on")

        headers = await reg.incomplete_sheet_headers()

        active = await reg.active_sheet_headers()
        expected = reg.INCOMPLETE_BASE_HEADERS + [
            h for h in active if h not in reg._INCOMPLETE_EXCLUDED_HEADERS
        ]
        assert headers == expected

    asyncio.run(go())


# ── Group 2/3: short mode merges in __short-only and global-only columns ─────────────────

def test_short_mode_short_only_column_is_present(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await db.set_setting("reg_q_city", "off")
        await db.set_setting("reg_q_city__short", "on")

        headers = await reg.incomplete_sheet_headers()
        assert "Город" in headers

    asyncio.run(go())


def test_short_mode_global_only_column_still_present_union_not_replace(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await db.set_setting("reg_q_university", "on")
        # no reg_q_university__short key set at all (absent → short gate resolves to False)

        headers = await reg.incomplete_sheet_headers()
        assert "ВУЗ" in headers

    asyncio.run(go())


# ── Group 4: toggle rollback must not collapse the merge (the whole point of this plan) ──

def test_toggle_rollback_keeps_merge_until_reg_started_row_cleared(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await db.set_setting("reg_q_city", "off")
        await db.set_setting("reg_q_city__short", "on")
        await db.mark_reg_started(1, "u", "short")

        # manager reverts the toggle on 2026-08-11
        await db.set_setting("registration_mode", "full")

        headers = await reg.incomplete_sheet_headers()
        assert "Город" in headers  # still merged — live abandoned promo row exists

        await db.clear_reg_started(1)

        headers_after_clear = await reg.incomplete_sheet_headers()
        active = await reg.active_sheet_headers()
        expected = reg.INCOMPLETE_BASE_HEADERS + [
            h for h in active if h not in reg._INCOMPLETE_EXCLUDED_HEADERS
        ]
        assert headers_after_clear == expected
        assert "Город" not in headers_after_clear

    asyncio.run(go())


# ── Group 5: has_short_incomplete() itself ────────────────────────────────────────────────

def test_has_short_incomplete_matrix(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        assert await db.has_short_incomplete() is False

        await db.mark_reg_started(1, "u", "full")
        assert await db.has_short_incomplete() is False

        await db.mark_reg_started(2, "u2", "party_overnight")
        assert await db.has_short_incomplete() is False

        await db.mark_reg_started(3, "u3", "short")
        assert await db.has_short_incomplete() is True

    asyncio.run(go())


# ── Group 6: no duplicates, order follows SHEET_COLUMNS ───────────────────────────────────

def test_merged_headers_no_duplicates_and_sheet_columns_order(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await db.set_setting("reg_q_city__short", "on")
        await db.set_setting("reg_q_university", "on")
        await db.set_setting("reg_q_phone", "on")
        await db.set_setting("reg_q_phone__short", "on")

        headers = await reg.incomplete_sheet_headers()
        assert len(headers) == len(set(headers))

        # order must follow SHEET_COLUMNS for everything after the fixed base prefix
        sheet_order = [h for h, _g, _f in reg.SHEET_COLUMNS]
        tail = headers[len(reg.INCOMPLETE_BASE_HEADERS):]
        filtered_sheet_order = [h for h in sheet_order if h in tail]
        assert tail == filtered_sheet_order

    asyncio.run(go())


# ── Group 7: incomplete_sheet_row projects merged short-only column value ────────────────

def test_incomplete_sheet_row_projects_short_only_field(tmp_path):
    _use_tmp(tmp_path)
    import json

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await db.set_setting("reg_q_city", "off")
        await db.set_setting("reg_q_city__short", "on")

        headers = await reg.incomplete_sheet_headers()
        partial_json = json.dumps({"full_name": "Иванов", "city": "Казань"}, ensure_ascii=False)
        row = reg.incomplete_sheet_row(1, "vasya", "2026-08-08 10:00:00", "city", partial_json, headers)
        values = dict(zip(headers, row))
        assert values["Город"] == "Казань"

    asyncio.run(go())


# ── Group 8: consumer parity — both callers still route through incomplete_sheet_headers ──

def test_admin_export_and_scheduler_sync_both_call_shared_helper():
    import handlers.admin as admin_mod
    import services.scheduler as scheduler_mod

    admin_src = inspect.getsource(admin_mod.export_incomplete)
    assert "incomplete_sheet_headers" in admin_src
    assert "incomplete_sheet_row" in admin_src

    scheduler_src = inspect.getsource(scheduler_mod.sync_incomplete_sheet_job)
    assert "incomplete_sheet_headers" in scheduler_src
    assert "incomplete_sheet_row" in scheduler_src
