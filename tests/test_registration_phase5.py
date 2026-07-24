"""Phase 5 Plan 2 (Participant Tracks — question engine) tests.

pytest-asyncio is unavailable in this env, so each test drives the async reg/db
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file — same
convention as tests/test_registration_phase4.py and tests/test_db_phase5.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

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
    # 6 at Phase-5 close; +reg_q_alumni_status (quick 260721-msh).
    assert len(p["on"]) == 7
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


def test_apply_party_preset_writes_every_reg_flow_step_except_overnight_exempt(tmp_path):
    """After _apply_party_preset(), every REG_FLOW step has an explicit __party key EXCEPT
    the WR-03 overnight-only trio (housing/bed_sharing/bed_partner), which stays at inherit
    so D-08's overnight/no-overnight skip rule keeps governing them."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await reg._apply_party_preset()
        distinct = set()
        for _step_key, setting_key, *_rest in reg.REG_FLOW:
            val = await db.get_setting(f"{setting_key}__party")
            if setting_key in reg._PARTY_PRESET_OVERNIGHT_EXEMPT:
                assert val is None
                continue
            assert val in ("on", "off")
            distinct.add(f"{setting_key}__party")
        assert len(distinct) == len(reg.REG_FLOW) - len(reg._PARTY_PRESET_OVERNIGHT_EXEMPT)

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


# ── WR-03 regression: party preset must not force off overnight housing/bed questions ──

def test_apply_party_preset_never_writes_overnight_trio_keys(tmp_path):
    """The exact bug: pre-fix, _apply_party_preset wrote reg_q_housing__party=off (and the
    same for bed_sharing/bed_partner) unconditionally, even though none of the three are in
    REG_PRESETS["party"]["on"] — defeating D-08 in any config where they are globally on."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await reg._apply_party_preset()
        for k in ("reg_q_housing", "reg_q_bed_sharing", "reg_q_bed_partner"):
            assert await db.get_setting(f"{k}__party") is None

    asyncio.run(go())


def test_party_preset_leaves_overnight_guest_asked_when_globally_on(tmp_path):
    """D-08 end-to-end: with housing/bed_sharing/bed_partner enabled GLOBALLY (RusCo-style
    conf config) and the party preset applied, an overnight guest is STILL asked — the
    __party keys are absent (inherit), so the global "on" flows through."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_housing", "on")
        await db.set_setting("reg_q_bed_sharing", "on")
        await db.set_setting("reg_q_bed_partner", "on")
        await reg._apply_party_preset()
        steps = await reg._get_enabled_steps({
            "participant_type": "party_overnight", "arrival": "Заранее",
        })
        assert "housing" in steps
        assert "bed_sharing" in steps

    asyncio.run(go())


def test_party_preset_leaves_noovernight_guest_skipped_when_globally_on(tmp_path):
    """Same globally-on config, but a no-overnight guest must still skip housing/bed via the
    existing D-08 overnight-only conditional skip rule — the preset fix does not disturb it."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_housing", "on")
        await db.set_setting("reg_q_bed_sharing", "on")
        await db.set_setting("reg_q_bed_partner", "on")
        await reg._apply_party_preset()
        steps = await reg._get_enabled_steps({
            "participant_type": "party_noovernight", "arrival": "Заранее",
        })
        assert "housing" not in steps
        assert "bed_sharing" not in steps
        assert "bed_partner" not in steps

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


# ── Plan 4 Task 3: _should_show_fork pre-flow gate (D-09, D-10) ─────────────────

_INPUT_COMBOS = [
    (party_track, recovered_track, is_registered)
    for party_track in (None, "party_overnight")
    for recovered_track in (None, "party_noovernight")
    for is_registered in (False, True)
]


def test_should_show_fork_false_when_fork_question_unset_for_every_combo(tmp_path):
    # ROADMAP SC#5: with party_fork_question at its default (unset -> "off"), an ordinary
    # delegate sees no extra screen, regardless of the other inputs.
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_enabled", "on")
        for party_track, recovered_track, is_registered in _INPUT_COMBOS:
            assert await reg._should_show_fork(party_track, recovered_track, is_registered) is False

    asyncio.run(go())


def test_should_show_fork_false_when_deep_link_track_resolved_even_both_settings_on(tmp_path):
    # D-10: an authoritative deep-link (or recovered) track always suppresses the fork.
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")
        assert await reg._should_show_fork("party_overnight", None, False) is False
        assert await reg._should_show_fork(None, "party_noovernight", False) is False

    asyncio.run(go())


def test_should_show_fork_false_when_party_enabled_off_even_fork_question_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        # party_enabled left unset -> defaults "off"
        assert await reg._should_show_fork(None, None, False) is False

    asyncio.run(go())


def test_should_show_fork_false_when_already_registered(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")
        assert await reg._should_show_fork(None, None, True) is False

    asyncio.run(go())


def test_should_show_fork_true_when_both_settings_on_no_track_not_registered(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")
        assert await reg._should_show_fork(None, None, False) is True

    asyncio.run(go())


def test_reg_flow_entry_count_unchanged_from_phase5_start():
    # D-09: Phase 5 itself added no new REG_FLOW step keys — 42 matches the count recorded in
    # the 05-02 SUMMARY ("threads participant_type to all 42 internal _prompt call sites").
    # Post-phase additions bump this deliberately: +alumni_status (quick 260721-msh) → 43.
    assert len(reg.REG_FLOW) == 43


def test_party_pick_token_vocabulary_matches_extract_party_track():
    # Same closed vocabulary drives both the deep-link extractor and the fork handler.
    assert reg._extract_party_track("party_over") == reg._PARTY_TAG_MAP["party_over"]
    assert reg._extract_party_track("party_noover") == reg._PARTY_TAG_MAP["party_noover"]


# ── CR-01 regression: fork must not drop referrer_id/source_tag attribution ─────
#
# cmd_start extracts referrer_id/source_tag BEFORE the party_fork_question gate. When the
# fork is shown, cmd_start returns without calling _start_registration_flow — the FIRST call
# for the session happens later, from party_pick/party_fallback_full, with an FSM state that
# (pre-fix) never saw referrer_id/source_tag at all. These tests drive cmd_start -> party_pick
# through a REAL FSMContext/MemoryStorage (same key aiogram would use for one chat/user) so the
# state handoff between the two handlers is exercised for real, not mocked.

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

    async def answer(self, *a, **k):
        return None

    async def answer_photo(self, *a, **k):
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username)
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        # Stands in for the bot's own fork message; party_pick/party_fallback_full swap in
        # callback.from_user via model_copy() before calling _start_registration_flow.
        self.message = _FakeMessage(0)

    async def answer(self, text=None, show_alert=False):
        return None


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def test_cr01_fork_persists_referrer_id_immediately(tmp_path):
    """The fork branch itself must write referrer_id/source/_source_from_tag into FSM state
    before returning — this is the exact bug: pre-fix, state stayed empty at this point."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")

        uid = 700001
        referrer = uid + 1
        state = _new_state(uid)

        class FakeCommand:
            args = str(referrer)

        await reg.cmd_start(_FakeMessage(uid, "referred"), state, bot=object(), command=FakeCommand())

        data = await state.get_data()
        assert data.get("referrer_id") == referrer

    asyncio.run(go())


class _CapturingMessage(_FakeMessage):
    """Records every answer/answer_photo text so a test can assert what the user was shown."""
    def __init__(self, uid, username=None):
        super().__init__(uid, username)
        self.texts = []

    async def answer(self, text=None, *a, **k):
        self.texts.append(text)
        return None

    async def answer_photo(self, *a, **k):
        self.texts.append("<photo>")
        return None


def test_me05_registered_user_not_locked_out_by_preselect_gate(tmp_path, monkeypatch):
    """ME-05: an already-registered (non-rejected) user must reach their menu even with
    pre-selection ON and a non-empty allowlist that excludes them — the gate is intake-only."""
    _use_tmp_db(tmp_path)
    from services import allowlist
    monkeypatch.setattr(allowlist, "_allowlist", {"someoneelse"})  # non-empty, excludes our user

    async def go():
        await db.init_db()
        await db.set_setting("preselect_enabled", "on")
        await db.set_setting("preselect_fail_text", "SENTINEL_FAIL")
        await db.set_setting("preselect_no_username_text", "SENTINEL_NOUSER")
        uid = 710001
        await db.add_user({"telegram_id": uid, "full_name": "Approved One",
                           "username": "notallowed", "registration_date": "2026-07-01 10:00:00"})
        await db.set_user_status(uid, "approved")
        msg = _CapturingMessage(uid, "notallowed")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        # Gate must NOT have fired — no lockout text shown to an existing approved delegate.
        assert "SENTINEL_FAIL" not in msg.texts
        assert "SENTINEL_NOUSER" not in msg.texts

    asyncio.run(go())


def test_me05_new_user_still_gated_by_preselect(tmp_path, monkeypatch):
    """Companion: the gate must STILL lock out a brand-new (unregistered) excluded user —
    the ME-05 bypass must be scoped to already-registered rows only, not a blanket disable."""
    _use_tmp_db(tmp_path)
    from services import allowlist
    monkeypatch.setattr(allowlist, "_allowlist", {"someoneelse"})

    async def go():
        await db.init_db()
        await db.set_setting("preselect_enabled", "on")
        await db.set_setting("preselect_fail_text", "SENTINEL_FAIL")
        uid = 710002
        msg = _CapturingMessage(uid, "notallowed")  # no users row → new intake
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        assert "SENTINEL_FAIL" in msg.texts  # gate fired for the new excluded user

    asyncio.run(go())


def test_high01_bare_restart_on_fork_preserves_referrer(tmp_path):
    """HIGH-01: a referred user who lands on the fork, then re-sends a BARE /start (no deep-link)
    while the fork is shown, must keep their original referrer — the second cmd_start must not
    clobber the saved referrer_id with None."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")

        uid = 720001
        referrer = uid + 1
        state = _new_state(uid)

        class RefCommand:
            args = str(referrer)

        class BareCommand:
            args = None

        # 1st /start via deep link → fork shown, referrer persisted.
        await reg.cmd_start(_FakeMessage(uid, "ref"), state, bot=object(), command=RefCommand())
        assert (await state.get_data()).get("referrer_id") == referrer

        # 2nd, bare /start while fork still displayed → must NOT clobber the referrer.
        await reg.cmd_start(_FakeMessage(uid, "ref"), state, bot=object(), command=BareCommand())
        assert (await state.get_data()).get("referrer_id") == referrer

    asyncio.run(go())


def test_cr01_referred_user_picks_full_still_lands_with_referrer_id(tmp_path):
    """The explicit "picks full" case CR-01 calls out: a referred user who lands on the fork
    and taps "Полная регистрация" must still finalize with referrer_id attribution intact."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")

        uid = 700002
        referrer = uid + 1
        state = _new_state(uid)

        class FakeCommand:
            args = str(referrer)

        await reg.cmd_start(_FakeMessage(uid, "referred2"), state, bot=object(), command=FakeCommand())

        await reg.party_pick(_FakeCallback("party_pick:full", uid, "referred2"), state)

        final_data = await state.get_data()
        assert final_data.get("referrer_id") == referrer
        assert final_data.get("participant_type") == "full"

        # Mirror finalize_registration: FSM data flows directly into add_user(data).
        row = dict(final_data)
        row["telegram_id"] = uid
        row["registration_date"] = "2026-07-21 12:00:00"
        await db.add_user(row)
        user = await db.get_user(uid)
        assert user["referrer_id"] == referrer

    asyncio.run(go())


def test_cr01_source_tagged_user_picks_party_track_still_lands_with_source(tmp_path):
    """Same bug, source_tag flavor: a src_-tagged user who forks into a PARTY track must not
    lose the campaign tag either — _source_from_tag must also survive so the "Источник"
    question stays skipped."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("party_enabled", "on")

        uid = 700003
        state = _new_state(uid)

        class FakeCommand:
            args = "src_vk"

        await reg.cmd_start(_FakeMessage(uid, "tagged"), state, bot=object(), command=FakeCommand())

        data_after_fork = await state.get_data()
        assert data_after_fork.get("source") == "vk"
        assert data_after_fork.get("_source_from_tag") is True

        await reg.party_pick(_FakeCallback("party_pick:party_over", uid, "tagged"), state)

        final_data = await state.get_data()
        assert final_data.get("source") == "vk"
        assert final_data.get("_source_from_tag") is True
        assert final_data.get("participant_type") == "party_overnight"

        row = dict(final_data)
        row["telegram_id"] = uid
        row["registration_date"] = "2026-07-21 12:00:00"
        await db.add_user(row)
        user = await db.get_user(uid)
        assert user["source"] == "vk"

    asyncio.run(go())
