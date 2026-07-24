---
phase: 06-settings-schema-registry
plan: 07
subsystem: config
tags: [settings-registry, admin-ui, aiogram3, toggle, enum, testing, regression]

# Dependency graph
requires:
  - phase: 06-settings-schema-registry
    provides: "SETTINGS_SCHEMA registry + get_setting_typed accessor (06-01); admin consumer wiring (06-05); reg/payment/scheduler consumer wiring (06-06)"
provides:
  - "Full-phase coverage closure: every consumer read-site for a key present in SETTINGS_SCHEMA resolves through get_setting_typed, byte-for-byte, with zero remaining raw `get_setting(key) or \"<default>\"` idiom for a registered key"
  - "06-SMOKE-CHECKLIST.md — automated regression totals (397/397), coverage-sweep finding + resolution, and the full human-smoke checklist (deferred-UAT, pending post-SumMeet execution)"
  - "REG-01/REG-02/REG-03 marked complete in REQUIREMENTS.md"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Final-coverage sweep as a distinct plan step: grep every raw `get_setting(...)` call site across all consumer files, cross-reference each key against SETTINGS_SCHEMA, and classify each hit as (a) already-migrated, (b) legitimately out-of-registry (key not yet in the schema), (c) legitimately raw (text/list/photo/file field, D-07), or (d) a genuine remaining gap — rather than assuming migration is complete once the plan's own named interfaces are done"
    - "Deferred-UAT pattern reused a third time (05-03/05-04 precedent): a blocking human-verify checkpoint's live-bot walk can be explicitly deferred (not skipped) when automated proof is otherwise exhaustive and live execution would collide with a hard external deadline (pre-forum code freeze), recorded as an explicit DEFERRED status with a concrete resume trigger, not silently dropped"

key-files:
  created: [.planning/phases/06-settings-schema-registry/06-SMOKE-CHECKLIST.md]
  modified: [handlers/admin.py, tests/test_settings_groups_c0x.py]

key-decisions:
  - "Decision 1 (orchestrator/user, post-checkpoint): CLOSE the 4 flagged raw-idiom sites now rather than leave them as a documented residual. Executed test-first (RED via inspect.getsource wiring gate + value-equivalence, GREEN via the migration) rather than skipped as originally recommended-but-deferred in the checkpoint return."
  - "Decision 2 (orchestrator/user, post-checkpoint): DEFER the live-bot smoke walk (Section 3 of 06-SMOKE-CHECKLIST.md) to post-SumMeet (forum 31 Jul-2 Aug 2026), following the same deferred-UAT pattern already used in Phase 5 (05-03/05-04). Automated regression net (397/397) stands as the full extent of pre-forum proof in a no-CI/linter environment."
  - "The coverage sweep found registration_mode/full_approval/short_approval/party_approval/payment_enabled/consent_enabled/party_enabled/party_fork_question/reg_university_mode/edu_conditional/reg_show_progress/payment_reminders_enabled all flowing through 3 shared multi-key helpers (_toggle_approval_setting, _toggle_module_setting, _toggle_value_setting) plus render_settings_text's own registration_mode read — all 4 sites migrated in one small, low-risk pass since each key's registry default was already proven byte-identical to the helper's own literal fallback (06-01 D-15 parse-equivalence contract)."
  - "No other gaps found in the sweep: preselect_enabled/nudge_enabled/pending_reminder_enabled remain on raw get_setting by design (never part of the D-09 toggle set, out of phase scope); all raw text/int/list/photo/file field reads are the intended pattern per D-07 (registry only adds typed parsing for toggle/int/enum/date types)."

patterns-established: []

requirements-completed: [REG-01, REG-02, REG-03]

# Metrics
duration: 60min
completed: 2026-07-24
---

# Phase 6 Plan 7: Final Coverage + Smoke Checklist Summary

**Closed the last 4 raw-idiom consumer read-sites in `handlers/admin.py` (render_settings_text's own registration_mode read + the 3 shared toggle helpers `_toggle_approval_setting`/`_toggle_module_setting`/`_toggle_value_setting`), bringing REG-02 to full-phase closure (397/397 tests), and recorded the mandatory live-bot smoke checklist as explicitly deferred to post-SumMeet.**

## Performance

- **Duration:** ~60 min (including a blocking human-decision checkpoint pause between Task 1 and the post-checkpoint closure work)
- **Started:** 2026-07-24T17:23:05+03:00 (approx, Task 1 commit)
- **Completed:** 2026-07-24T18:22:47+03:00
- **Tasks:** 2 of 2 (Task 1 automated; Task 2 checkpoint resolved via orchestrator/user decision, not live execution)
- **Files modified:** 3 (`.planning/phases/06-settings-schema-registry/06-SMOKE-CHECKLIST.md`, `handlers/admin.py`, `tests/test_settings_groups_c0x.py`)

## Accomplishments

- **Task 1 (automated):** ran the full plan-scope regression suite (149/149) and the full repo suite (395/395), both green — no drift from any prior wave. Created `06-SMOKE-CHECKLIST.md` recording these totals plus the full human smoke checklist (landing screen, mandatory 14-button before/after comparison table, 5 migrated group sub-screens, edit round-trip, default-fallback display, reg_q_* toggle, scheduler timing spot-check, restart-persistence).
- **Coverage sweep:** grepped every raw `await get_setting(...)` call site across `handlers/admin.py`, `handlers/registration.py`, `handlers/payment.py`, `services/scheduler.py`, `services/reminders.py`, `keyboards/builders.py` and cross-referenced each key against `SETTINGS_SCHEMA`. Found exactly 4 genuine remaining gaps, all in `handlers/admin.py`, all confined to shared toggle-handler helpers (12 toggle buttons fan out from these 3 helpers).
- **Checkpoint reached and resolved:** the human-verify checkpoint (live-bot smoke) was surfaced with the coverage-sweep finding documented for a decision. The orchestrator/user made two decisions in the same turn: (1) close the 4 flagged sites now, test-first; (2) defer the live smoke walk to post-SumMeet.
- **Closure (post-checkpoint):** added a RED wiring test (`test_generic_toggle_helpers_wired_to_registry`, confirmed failing before migration) plus a PASS-first value-equivalence test (`test_toggle_current_value_equiv_across_generic_helpers`, matching the established 06-05 byte-for-byte-preservation pattern) to `tests/test_settings_groups_c0x.py`. Migrated all 4 sites in `handlers/admin.py` to `get_setting_typed`. Confirmed GREEN: 31/31 in the settings test file, 397/397 full suite, clean import smoke.
- **REG-01/REG-02/REG-03 marked complete** in `REQUIREMENTS.md` — this is the final plan of Phase 6 (7/7 plans done), and the coverage sweep confirms no consumer anywhere still hand-rolls a raw idiom for a registered key.

## Task Commits

Each task/step was committed atomically:

1. **Task 1: Run the full automated regression net and record results** - `253681e` (docs) — 149/149 plan-scope, 395/395 full suite, checklist created with sweep finding documented (unresolved at that point)
2. **Checkpoint decision 1, step A (RED):** `a4710e0` (test) — wiring gate confirmed failing before migration
3. **Checkpoint decision 1, step B (GREEN):** `602a611` (feat) — migrated all 4 sites, 397/397 full suite green
4. **Checkpoint decision 2 + closure record:** `c31c978` (docs) — recorded boundary closure and deferred-UAT sign-off in the checklist

**Plan metadata:** (this commit, following this Summary)

_Note: the checkpoint's own decision-making happened via an orchestrator/user round-trip mid-plan, not a standard TDD task-level cycle — but the actual code-migration work (steps 2-3 above) itself followed the RED→GREEN discipline this project uses for all behavior-preserving migrations._

## Files Created/Modified

- `.planning/phases/06-settings-schema-registry/06-SMOKE-CHECKLIST.md` - automated regression totals (149/149, then 397/397 after closure), the coverage-sweep finding + full resolution narrative, the 8-section human smoke checklist (unchecked, deferred), and the final deferred-UAT sign-off
- `handlers/admin.py` - 4 read-sites migrated to `get_setting_typed`: `render_settings_text`'s own `registration_mode` read; `_toggle_approval_setting` (`full_approval`/`short_approval`/`party_approval`); `_toggle_module_setting` (`payment_enabled`/`consent_enabled`/`party_enabled`/`party_fork_question`); `_toggle_value_setting` (`reg_university_mode`/`edu_conditional`/`reg_show_progress`/`payment_reminders_enabled`). No button text, callback_data, or flip-logic structure changed — only the current-value read primitive.
- `tests/test_settings_groups_c0x.py` - added `test_toggle_current_value_equiv_across_generic_helpers` (value-equivalence matrix across all 11 affected toggle handlers × {None, "", each enum option}) and `test_generic_toggle_helpers_wired_to_registry` (source-inspection wiring gate, RED before / GREEN after)

## Decisions Made

- **Decision 1 (orchestrator/user):** close the 4 flagged sites now rather than leave as a documented residual for a future follow-up plan. Rationale given: the migration is small (4 read-site swaps in already-known-safe shared helpers), low-risk (registry defaults already proven byte-identical), and test-first execution keeps it auditable.
- **Decision 2 (orchestrator/user):** defer the live-bot smoke walk (Section 3) to post-SumMeet rather than block plan completion on it. Rationale given: execution is pre-forum (31 Jul-2 Aug 2026); the automated regression net is exhaustive for a no-CI/linter environment; this mirrors the Phase 5 deferred-UAT precedent (05-03/05-04) already established in this codebase.
- **Scope discipline maintained:** the coverage sweep also surfaced 3 keys (`preselect_enabled`, `nudge_enabled`, `pending_reminder_enabled`) still on raw `get_setting` — these were explicitly NOT touched, since they were never part of the D-09 toggle set migrated into `SETTINGS_SCHEMA` in the first place (out of this phase's scope by design, not an oversight). Migrating them would be adding new registry entries, a different kind of change than closing an existing flagged read-site gap.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical, escalated to orchestrator per plan's own instruction, then executed on orchestrator decision] Closed the 06-05/06-06-flagged raw-idiom boundary**
- **Found during:** Task 1 (coverage sweep, part of the plan's own instructed investigation)
- **Issue:** 4 consumer read-sites in `handlers/admin.py` still used `get_setting(key) or "<default>"` for keys present in `SETTINGS_SCHEMA`, contradicting the phase's "single source of truth" north star, though with zero live-behavior risk.
- **Fix:** Per the executing agent's explicit instruction, this was NOT auto-fixed inline — it was surfaced at the checkpoint for an explicit decision (structural/architectural-adjacent change touching 3 shared helpers fanning out to 12 buttons). The orchestrator/user then explicitly decided to close it, at which point it was executed test-first (RED/GREEN).
- **Files modified:** `handlers/admin.py`, `tests/test_settings_groups_c0x.py`
- **Verification:** RED confirmed before migration; GREEN confirmed after (31/31 settings-file tests, 397/397 full suite, clean import smoke)
- **Committed in:** `a4710e0` (RED), `602a611` (GREEN)

---

**Total deviations:** 1 (surfaced per Rule 4 discipline at the checkpoint, then executed per explicit orchestrator/user decision — not a silent scope expansion)
**Impact on plan:** Closes REG-02 fully; no scope creep beyond the explicitly-decided 4 sites; the 3 genuinely out-of-registry keys (`preselect_enabled`/`nudge_enabled`/`pending_reminder_enabled`) were correctly left untouched.

## Issues Encountered

None. The RED wiring test failed exactly as expected before migration (proving the gate was meaningful, not a rubber-stamp), and the value-equivalence test passed both before and after (as expected for a byte-for-byte preserving swap of only the read primitive).

## User Setup Required

None — no external service configuration required. No `bot_settings` DB migration needed; the 4 migrated read-sites only change where the current-value read is resolved from (registry vs. raw `get_setting`), not what value is read or written — confirmed by the value-equivalence test covering every affected key across {None, "", each of its valid options}.

## Next Phase Readiness

- **Phase 6 (settings-schema-registry) is now feature-complete**: all 7 plans done, REG-01/REG-02/REG-03 closed, 397/397 automated tests green.
- **Live smoke walk (06-SMOKE-CHECKLIST.md §3) is the one remaining open item**, explicitly deferred to post-SumMeet (31 Jul-2 Aug 2026) per orchestrator/user decision. The next session after the forum should walk all 8 sub-sections, with special attention to the mandatory 14-button before/after comparison table (§3.2) and the restart-persistence check (§3.8), and record PASS/FAIL per row in the same file.
- No blockers. The phase's own north star ("настройка через один файл") is now structurally true for every migrated key: `SETTINGS_SCHEMA` is the sole metadata+default source, and every consumer read for a registered key — display, behavioral gate, and toggle-handler current-value read alike — resolves through `get_setting_typed`.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: .planning/phases/06-settings-schema-registry/06-SMOKE-CHECKLIST.md
- FOUND: handlers/admin.py (get_setting_typed migrated at the 4 flagged sites)
- FOUND: tests/test_settings_groups_c0x.py (2 new tests)
- FOUND: 253681e (Task 1 commit)
- FOUND: a4710e0 (RED test commit)
- FOUND: 602a611 (GREEN migration commit)
- FOUND: c31c978 (checklist boundary-closure/deferred-UAT commit)
