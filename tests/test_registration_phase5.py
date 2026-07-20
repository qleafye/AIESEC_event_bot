"""Phase 5 Plan 2 (Participant Tracks — question engine) tests.

pytest-asyncio is unavailable in this env, so each test drives the async reg/db
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file — same
convention as tests/test_registration_phase4.py and tests/test_db_phase5.py.
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


# ── Task 1: _is_step_enabled_for_track tri-state resolver (D-03, D-04) ──────────

def test_full_track_matches_is_step_enabled_when_global_unset(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        expected = await reg._is_step_enabled("reg_q_age")
        actual = await reg._is_step_enabled_for_track("reg_q_age", "full")
        assert actual == expected

    asyncio.run(go())


def test_full_track_matches_is_step_enabled_when_global_set(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age", "off")
        expected = await reg._is_step_enabled("reg_q_age")
        actual = await reg._is_step_enabled_for_track("reg_q_age", "full")
        assert actual == expected

    asyncio.run(go())


def test_none_track_matches_is_step_enabled():
    async def go():
        expected = await reg._is_step_enabled("reg_q_age")
        actual = await reg._is_step_enabled_for_track("reg_q_age", None)
        assert actual == expected

    asyncio.run(go())


def test_party_inherits_when_override_absent_global_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        # reg_q_age unset globally -> REG_DEFAULTS says "on"; __party absent -> inherit True.
        assert await reg._is_step_enabled_for_track("reg_q_age", "party_overnight") is True

    asyncio.run(go())


def test_party_inherits_when_override_absent_global_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age", "off")
        # __party absent -> inherit global "off".
        assert await reg._is_step_enabled_for_track("reg_q_age", "party_overnight") is False

    asyncio.run(go())


def test_party_override_wins_no_cross_contamination(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age", "off")
        await db.set_setting("reg_q_age__party", "on")
        assert await reg._is_step_enabled_for_track("reg_q_age", "party_overnight") is True
        # full track still resolves to the global value, untouched by the __party write.
        assert await reg._is_step_enabled_for_track("reg_q_age", "full") is False

    asyncio.run(go())


def test_party_override_off_full_still_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age", "on")
        await db.set_setting("reg_q_age__party", "off")
        assert await reg._is_step_enabled_for_track("reg_q_age", "party_overnight") is False
        assert await reg._is_step_enabled_for_track("reg_q_age", "full") is True

    asyncio.run(go())


def test_single_party_key_governs_both_subtracks(tmp_path):
    """D-03: one __party namespace covers BOTH party_overnight and party_noovernight."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age__party", "off")
        assert await reg._is_step_enabled_for_track("reg_q_age", "party_overnight") is False
        assert await reg._is_step_enabled_for_track("reg_q_age", "party_noovernight") is False

    asyncio.run(go())


# ── Task 1: _get_enabled_steps threads participant_type (D-08) ──────────────────

def test_party_noovernight_never_sees_overnight_steps(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        # Turn every question on so the overnight-only rule is the only thing under test.
        for k in reg.REG_DEFAULTS:
            await db.set_setting(k, "on")
        steps = await reg._get_enabled_steps({"participant_type": "party_noovernight"})
        assert "housing" not in steps
        assert "bed_sharing" not in steps
        assert "bed_partner" not in steps

    asyncio.run(go())


def test_party_overnight_may_see_housing(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        for k in reg.REG_DEFAULTS:
            await db.set_setting(k, "on")
        steps = await reg._get_enabled_steps({"participant_type": "party_overnight", "arrival": "Заранее"})
        assert "housing" in steps
        assert "bed_sharing" in steps

    asyncio.run(go())


def test_full_track_get_enabled_steps_unchanged_empty_data(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        empty = await reg._get_enabled_steps({})
        full = await reg._get_enabled_steps({"participant_type": "full"})
        assert empty == full

    asyncio.run(go())


def test_full_track_regression_unaffected_by_party_override(tmp_path):
    """Full-track regression: a __party override on the opposite value must not change
    _get_enabled_steps for a full-track user."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        before = await reg._get_enabled_steps({"participant_type": "full"})
        await db.set_setting("reg_q_age__party", "off")
        after = await reg._get_enabled_steps({"participant_type": "full"})
        assert before == after
        assert "age" in after  # reg_q_age defaults "on" and full track never reads __party

    asyncio.run(go())
