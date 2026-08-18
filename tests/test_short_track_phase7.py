"""Phase 7 Plan 1 (Configurable short registration form — engine) tests.

pytest-asyncio is unavailable in this env, so async helpers are driven via asyncio.run()
and config.DB_PATH is pointed at a tmp_path file — same convention as
tests/test_registration_phase5.py.

Fixture/monkeypatch structure copied from tests/test_registration_phase5.py (fake
message/state/callback) and tests/test_sheets_phase5.py (_FakeState with plain-dict
backing, finalize_registration monkeypatch idiom).
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg
# Phase 13 REFAC (13-03): process_full_name moved to handlers/reg_steps.py -- imported
# separately since it decorates the SAME shared reg.router but resolves its own
# finalize_registration/_get_enabled_steps calls via reg_steps's own module globals.
from handlers import reg_steps


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeMessage:
    def __init__(self, uid, text, username=None):
        self.from_user = _FakeUser(uid, username)
        self.text = text
        self.answers = []

    async def answer(self, *a, **k):
        self.answers.append((a, k))


class _FakeState:
    """Plain-dict backed FSMContext stand-in — get_data/update_data/clear only, matching
    the subset process_full_name actually calls."""

    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def clear(self):
        self._data = {}


# ── Group 1: SHORT-04 main regression — zero __short keys behaves byte-identical to today ──

def test_zero_short_keys_yields_no_enabled_steps(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        enabled = await reg._get_enabled_steps({"participant_type": "short"})
        assert enabled == []

    asyncio.run(go())


def test_process_full_name_short_zero_keys_finalizes_without_asking(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    finalize_calls = []
    ask_calls = []

    async def fake_finalize(message, state, bot):
        finalize_calls.append((message, state, bot))

    async def fake_ask_step(*a, **k):
        ask_calls.append((a, k))

    monkeypatch.setattr(reg_steps, "finalize_registration", fake_finalize)
    monkeypatch.setattr(reg, "_ask_step", fake_ask_step)

    message = _FakeMessage(600001, "Иванов Иван")
    state = _FakeState({"participant_type": "short"})

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "short")
        await reg_steps.process_full_name(message, state, bot=None)

    asyncio.run(go())

    assert len(finalize_calls) == 1
    assert len(ask_calls) == 0


# ── Group 2: gate turns a question on, isolated from the global toggle ─────────────────────

def test_short_phone_override_isolated_from_global(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_phone__short", "on")
        enabled = await reg._get_enabled_steps({"participant_type": "short"})
        assert enabled == ["phone"]
        # Global reg_q_phone stays at its own default (off) — untouched by the __short write.
        assert await reg._is_step_enabled("reg_q_phone") is False

    asyncio.run(go())


# ── Group 3: `is not None` — absent key means "do not ask", never inherit the global ────────

def test_short_off_override_wins_over_global_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age", "on")
        await db.set_setting("reg_q_age__short", "off")
        enabled = await reg._get_enabled_steps({"participant_type": "short"})
        assert "age" not in enabled

    asyncio.run(go())


def test_short_absent_key_does_not_inherit_global_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_age", "on")
        # No reg_q_age__short written at all.
        enabled = await reg._get_enabled_steps({"participant_type": "short"})
        assert "age" not in enabled

    asyncio.run(go())


# ── Group 4: full form untouched by writing a batch of __short keys ─────────────────────────

def test_full_track_steps_unchanged_by_short_keys(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        before = await reg._get_enabled_steps({"participant_type": "full"})
        await db.set_setting("reg_q_phone__short", "on")
        await db.set_setting("reg_q_age__short", "on")
        await db.set_setting("reg_q_vk__short", "off")
        await db.set_setting("reg_q_city__short", "on")
        after = await reg._get_enabled_steps({"participant_type": "full"})
        assert before == after

    asyncio.run(go())


# ── Group 5: party track untouched by short-namespace writes ────────────────────────────────

def test_party_track_unaffected_by_short_namespace(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_city__party", "on")
        before = await reg._get_enabled_steps({"participant_type": "party_overnight"})
        assert "city" in before
        await db.set_setting("reg_q_city__short", "off")
        after = await reg._get_enabled_steps({"participant_type": "party_overnight"})
        assert before == after
        assert "city" in after

    asyncio.run(go())


# ── Group 6: "short" cannot leak into party gates/skip-rules (guarantee #4) ─────────────────

def test_is_party_track_rejects_short():
    assert reg._is_party_track("short") is False


def test_short_track_not_subject_to_party_housing_skip_rule(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_housing__short", "on")
        await db.set_setting("reg_q_bed_sharing__short", "on")
        enabled = await reg._get_enabled_steps({"participant_type": "short"})
        assert "housing" in enabled
        assert "bed_sharing" in enabled

    asyncio.run(go())


# ── Group 7: _resolve_track ──────────────────────────────────────────────────────────────────

def test_resolve_track_table(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()

        await db.set_setting("registration_mode", "short")
        assert await reg._resolve_track(None) == "short"
        assert await reg._resolve_track("full") == "short"
        assert await reg._resolve_track("party_overnight") == "party_overnight"

        await db.set_setting("registration_mode", "full")
        assert await reg._resolve_track(None) == "full"
        assert await reg._resolve_track("short") == "short"

    asyncio.run(go())


# ── Group 8: SHORT-05 basic — short branch resolves from short_setting, not party ──────────

def test_decide_status_short_basic():
    result = reg._decide_status(
        "short", full_setting="manual", short_setting="auto",
        participant_type="short", party_setting="manual",
    )
    assert result == "approved"

    result = reg._decide_status(
        "short", full_setting="auto", short_setting="manual",
        participant_type="short", party_setting="auto",
    )
    assert result == "pending"


# ── Group 9: SHORT-05 drift — decision follows the persisted track, not live reg_mode ───────

def test_decide_status_short_survives_mode_drift_to_full():
    # Manager already flipped the toggle back to "full"; delegate started under "short".
    result = reg._decide_status(
        "full", full_setting="manual", short_setting="auto",
        participant_type="short", party_setting="manual",
    )
    assert result == "approved"

    result = reg._decide_status(
        "full", full_setting="auto", short_setting="manual",
        participant_type="short", party_setting="manual",
    )
    assert result == "pending"


# ── Group 10: full-form / party branches of _decide_status did not move ────────────────────

def test_decide_status_full_and_none_branch_unchanged():
    for participant_type in ("full", None):
        for reg_mode in ("full", "short"):
            for full_setting in ("manual", "auto"):
                for short_setting in ("manual", "auto"):
                    expected_setting = full_setting if reg_mode == "full" else short_setting
                    expected = "pending" if expected_setting == "manual" else "approved"
                    actual = reg._decide_status(
                        reg_mode, full_setting, short_setting,
                        participant_type=participant_type, party_setting="auto",
                    )
                    assert actual == expected, (participant_type, reg_mode, full_setting, short_setting)


def test_decide_status_party_branch_unchanged():
    for participant_type in ("party_overnight", "party_noovernight"):
        for party_setting in ("manual", "auto", None):
            expected = "pending" if (party_setting or "manual") == "manual" else "approved"
            actual = reg._decide_status(
                "full", full_setting="auto", short_setting="auto",
                participant_type=participant_type, party_setting=party_setting,
            )
            assert actual == expected, (participant_type, party_setting)
