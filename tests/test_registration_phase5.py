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


# ── Task 2: per-track question wording via _prompt (D-05) ───────────────────────

def test_prompt_no_settings_returns_default(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await reg._prompt("housing", "default") == "default"

    asyncio.run(go())


def test_prompt_global_override_unchanged(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_prompt_housing", "Глобальный")
        assert await reg._prompt("housing", "default") == "Глобальный"

    asyncio.run(go())


def test_prompt_full_track_never_reads_party_key(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_prompt_housing", "Глобальный")
        await db.set_setting("reg_prompt_housing__party", "Партийный")
        assert await reg._prompt("housing", "default", "full") == "Глобальный"

    asyncio.run(go())


def test_prompt_party_track_reads_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_prompt_housing__party", "Партийный")
        assert await reg._prompt("housing", "default", "party_overnight") == "Партийный"

    asyncio.run(go())


def test_prompt_party_track_falls_back_to_global_when_override_absent(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_prompt_housing", "Глобальный")
        assert await reg._prompt("housing", "default", "party_overnight") == "Глобальный"

    asyncio.run(go())


def test_prompt_party_subtracks_share_same_override_key(tmp_path):
    """D-03: party_overnight and party_noovernight resolve the same __party key."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_prompt_housing__party", "Партийный")
        assert await reg._prompt("housing", "default", "party_overnight") == "Партийный"
        assert await reg._prompt("housing", "default", "party_noovernight") == "Партийный"

    asyncio.run(go())


def test_prompt_two_positional_args_still_work(tmp_path):
    """Every existing two-argument call site keeps compiling and behaving identically."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await reg._prompt("age", "Напиши свой возраст числом:") == "Напиши свой возраст числом:"

    asyncio.run(go())


def test_prompt_empty_party_override_falls_back_to_global(tmp_path):
    """T-05-02-05: truthiness (not is-not-None) — an empty __party override string must
    not strand the user on a blank message; it falls back to the global text."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_prompt_housing", "Глобальный")
        await db.set_setting("reg_prompt_housing__party", "")
        assert await reg._prompt("housing", "default", "party_overnight") == "Глобальный"

    asyncio.run(go())


# ── Task 3: 🎉 Party seed preset (D-07) ──────────────────────────────────────────

def test_party_preset_shape():
    p = reg.REG_PRESETS["party"]
    assert p["label"] == "🎉 Party"
    assert len(p["on"]) == 6
    assert all(k.startswith("reg_q_") for k in p["on"])
    assert "payment_enabled" not in p


def test_party_preset_keys_render_russian_labels():
    """Every seed key must exist in REG_LABELS so the shared confirm dialog in admin.py
    renders a Russian label instead of the raw internal key."""
    from handlers.admin import REG_LABELS
    missing = [k for k in reg.REG_PRESETS["party"]["on"] if k not in REG_LABELS]
    assert not missing, missing


def test_party_preset_keys_are_real_reg_flow_setting_keys():
    sk = {t[1] for t in reg.REG_FLOW}
    bad = [k for k in reg.REG_PRESETS["party"]["on"] if k not in sk]
    assert not bad, bad


def test_apply_party_preset_writes_every_reg_flow_step(tmp_path):
    """After _apply_party_preset(), every REG_FLOW step has an explicit __party key."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await reg._apply_party_preset()
        distinct = set()
        for _step_key, setting_key, *_rest in reg.REG_FLOW:
            val = await db.get_setting(f"{setting_key}__party")
            assert val in ("on", "off")
            distinct.add(f"{setting_key}__party")
        assert len(distinct) == len(reg.REG_FLOW)

    asyncio.run(go())


def test_apply_party_preset_isolation_global_keys_untouched(tmp_path):
    """D-07 isolation: applying the party preset must never change any global reg_q_*
    value — pre-set global values are byte-identical before and after the call."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        # Set a handful of global keys to known values, including some that overlap
        # with the party preset's "on" list and some that don't.
        await db.set_setting("reg_q_age", "off")
        await db.set_setting("reg_q_phone", "on")
        await db.set_setting("reg_q_university", "on")
        await db.set_setting("reg_q_food", "off")
        before = {
            "reg_q_age": await db.get_setting("reg_q_age"),
            "reg_q_phone": await db.get_setting("reg_q_phone"),
            "reg_q_university": await db.get_setting("reg_q_university"),
            "reg_q_food": await db.get_setting("reg_q_food"),
        }

        await reg._apply_party_preset()

        after = {
            "reg_q_age": await db.get_setting("reg_q_age"),
            "reg_q_phone": await db.get_setting("reg_q_phone"),
            "reg_q_university": await db.get_setting("reg_q_university"),
            "reg_q_food": await db.get_setting("reg_q_food"),
        }
        assert before == after

    asyncio.run(go())


def test_apply_party_preset_on_keys_are_explicitly_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await reg._apply_party_preset()
        for k in reg.REG_PRESETS["party"]["on"]:
            assert await db.get_setting(f"{k}__party") == "on"
        # A key NOT in the seed list must be explicitly "off" (determinism guarantee),
        # not merely absent.
        assert await db.get_setting("reg_q_university__party") == "off"

    asyncio.run(go())


# ── Plan 4 Task 1: _decide_status party branch (D-13, D-14) ─────────────────────

def test_decide_status_full_unchanged_no_track():
    # No track passed -> byte-identical to pre-Phase-5 behavior.
    assert reg._decide_status("full", "manual", "manual") == "pending"
    assert reg._decide_status("full", "auto", "manual") == "approved"


def test_decide_status_party_auto_wins_over_full_manual():
    # ROADMAP SC#6: party_approval=auto approves even though full_approval=manual.
    assert reg._decide_status("full", "manual", "manual", "party_overnight", "auto") == "approved"


def test_decide_status_party_manual_wins_over_full_short_auto():
    assert reg._decide_status("short", "auto", "auto", "party_noovernight", "manual") == "pending"


def test_decide_status_party_setting_ignored_for_full_track():
    # participant_type="full" -> party_setting is never consulted.
    assert reg._decide_status("full", "manual", "manual", "full", "auto") == "pending"


def test_decide_status_party_none_setting_defaults_to_manual():
    # D-13: an unconfigured party_approval must moderate, never silently auto-approve.
    assert reg._decide_status("full", "auto", "auto", "party_overnight", None) == "pending"
    assert reg._decide_status("full", "auto", "auto", "party_noovernight", None) == "pending"


def test_decide_status_party_overnight_and_noovernight_share_same_branch():
    assert reg._decide_status("full", "manual", "manual", "party_overnight", "auto") == "approved"
    assert reg._decide_status("full", "manual", "manual", "party_noovernight", "auto") == "approved"


def _finalize_row(tmp_path, telegram_id: int, participant_type: str, party_approval: str | None,
                   full_approval: str = "manual"):
    """Simulate the status-decision + persistence slice of finalize_registration (D-13/D-14)
    without driving the full FSM: resolve status via _decide_status, write the row via
    add_user, then set_user_status — the exact sequence finalize_registration performs."""
    async def go():
        await db.init_db()
        if party_approval is not None:
            await db.set_setting("party_approval", party_approval)
        await db.set_setting("full_approval", full_approval)
        data = {
            "telegram_id": telegram_id,
            "registration_date": "2026-07-20 12:00:00",
            "participant_type": participant_type,
            "full_name": "Тест Тестов",
        }
        await db.add_user(data)
        reg_mode = await db.get_setting("registration_mode") or "short"
        full_setting = await db.get_setting("full_approval") or "manual"
        short_setting = await db.get_setting("short_approval") or "auto"
        party_setting = await db.get_setting("party_approval")
        status = reg._decide_status(
            reg_mode, full_setting, short_setting,
            participant_type=data.get("participant_type", "full"), party_setting=party_setting,
        )
        await db.set_user_status(telegram_id, status)

    asyncio.run(go())


def test_party_auto_approval_never_enters_pending_queue(tmp_path):
    # D-14: with party_approval=auto (and full_approval=manual) a party application is
    # approved on submit and NEVER appears in the pending queue at all.
    _use_tmp_db(tmp_path)
    _finalize_row(tmp_path, 555001, "party_overnight", "auto", full_approval="manual")

    async def check():
        assert await db.get_pending_count() == 0
        user = await db.get_user(555001)
        assert user["status"] == "approved"

    asyncio.run(check())


def test_party_manual_approval_appears_in_shared_pending_queue(tmp_path):
    # D-14: with party_approval=manual the row lands in the SAME status='pending' queue as
    # full applications — no new query, no second screen.
    _use_tmp_db(tmp_path)
    _finalize_row(tmp_path, 555002, "party_noovernight", "manual", full_approval="auto")

    async def check():
        user = await db.get_user(555002)
        assert user["status"] == "pending"
        pending = await db.get_pending_users(limit=50)
        assert any(u["telegram_id"] == 555002 for u in pending)

    asyncio.run(check())


# ── Plan 4 Task 2: _approve_text_for per-track approval message (D-15) ──────────

def test_approve_text_for_full_track_uses_global_when_set(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("approve_text", "Глобальный текст одобрения")
        assert await reg._approve_text_for("full") == "Глобальный текст одобрения"

    asyncio.run(go())


def test_approve_text_for_full_track_falls_back_to_hardcoded_default(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await reg._approve_text_for("full") == reg.DEFAULT_APPROVE_TEXT

    asyncio.run(go())


def test_approve_text_for_party_uses_party_override_when_set(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("approve_text", "Глобальный текст одобрения")
        await db.set_setting("approve_text__party", "Добро пожаловать на вечеринку!")
        assert await reg._approve_text_for("party_overnight") == "Добро пожаловать на вечеринку!"

    asyncio.run(go())


def test_approve_text_for_party_falls_back_to_global_when_override_absent(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("approve_text", "Глобальный текст одобрения")
        assert await reg._approve_text_for("party_overnight") == "Глобальный текст одобрения"

    asyncio.run(go())


def test_approve_text_for_party_noovernight_shares_same_override_key(tmp_path):
    # D-03 single namespace: no __party_overnight / __party_noovernight split.
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("approve_text__party", "Добро пожаловать на вечеринку!")
        assert await reg._approve_text_for("party_noovernight") == "Добро пожаловать на вечеринку!"

    asyncio.run(go())


def test_approve_text_for_party_empty_override_falls_back_to_global(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("approve_text", "Глобальный текст одобрения")
        await db.set_setting("approve_text__party", "")
        assert await reg._approve_text_for("party_overnight") == "Глобальный текст одобрения"

    asyncio.run(go())


def test_send_completion_and_bonus_signature_has_participant_type():
    import inspect
    sig = inspect.signature(reg.send_completion_and_bonus)
    assert "participant_type" in sig.parameters
    assert sig.parameters["participant_type"].default is None


def test_approve_user_resolves_track_before_payment_branch():
    import inspect
    src = inspect.getsource(reg.approve_user)
    assert "get_user(" in src
    assert src.index("get_user(") < src.index("payment_enabled")
