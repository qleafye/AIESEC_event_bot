---
phase: 06-settings-schema-registry
plan: 05
subsystem: config
tags: [settings-registry, admin-ui, aiogram3, toggle, enum, feature-switches]

# Dependency graph
requires:
  - phase: 06-settings-schema-registry
    provides: "SETTINGS_SCHEMA toggles group (14 enum feature-switch keys, verified defaults) + get_setting_typed accessor (06-01, 06-04)"
provides:
  - "handlers/admin.py::render_settings_text — 10 feature-switch reads (reg_bonus_enabled, full_approval, short_approval, pending_notify_mode, payment_enabled, consent_enabled, payment_reminders_enabled, party_enabled, party_fork_question, party_approval) resolved through get_setting_typed"
  - "handlers/admin.py::build_settings_keyboard — 14 feature-switch reads (adds registration_mode, reg_university_mode, edu_conditional, reg_show_progress to the above set) resolved through get_setting_typed, button-text ternaries and callback_data strings byte-identical"
  - "4 admin toggle-handler read-sites (toggle_registration_mode, toggle_notify_mode, toggle_bonus, _refresh_party_sheet_header's party_enabled gate) resolve their current value through get_setting_typed; set_setting write paths untouched"
  - "Automated landing-text + toggle-button snapshot regression tests (tests/test_settings_groups_c0x.py) locking render output byte-for-byte over default + mixed cases"
affects: [06-06, 06-07]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Read-site migration without touching structure: swap `await get_setting(k) or '<literal>'` for `await get_setting_typed(k)` while keeping every surrounding ternary/ label / callback_data string byte-identical — proves the registry can absorb a consumer's reads without a UI-visible behavior change"
    - "Scope discipline via interfaces-block enumeration: only the 4 explicitly named handler read-sites were migrated; the shared multi-key toggle helpers (_toggle_module_setting, _toggle_approval_setting, _toggle_value_setting) and render_settings_text's own registration_mode read were left untouched because the plan's interfaces/truths lists explicitly excluded them from this wave"

key-files:
  created: []
  modified: [handlers/admin.py, tests/test_settings_groups_c0x.py]

key-decisions:
  - "D-12 honored: button-generation structure (bespoke settings_toggle_*/toggle_* callback_data strings, button-text ternaries) was NOT refactored into a generated loop — only the preceding value reads were swapped to get_setting_typed"
  - "Deviation-avoidance (Rule-1 boundary respected, not applied): render_settings_text's own `registration_mode` read (line 466, `reg_mode = await get_setting(\"registration_mode\") or \"short\"`) was left on the old idiom, matching the plan's truths bullet and interfaces list which explicitly enumerate render_settings_text's migrated keys WITHOUT registration_mode (build_settings_keyboard's registration_mode read at :528 was migrated per its own explicit interfaces entry). Not a bug — the plan scoped it this way twice (truths + interfaces), so no unrequested scope expansion was made."
  - "_toggle_module_setting/_toggle_approval_setting/_toggle_value_setting (generic multi-key handlers used by payment_enabled/consent_enabled/party_enabled/party_fork_question/reg_university_mode/edu_conditional/reg_show_progress toggles) were left on the old `get_setting(key) or default` idiom — the plan's interfaces block names only 4 specific single-key handler sites (registration_mode, pending_notify_mode, reg_bonus_enabled, the party_enabled gate) for this wave; these generic key-parameterized helpers are out of the explicit scope"

patterns-established: []

requirements-completed: []  # REG-02 is phase-wide (spans 06-01..06-07); this plan is a partial contribution (admin READ-SITE consumer wiring for render_settings_text/build_settings_keyboard/4 toggle-handler sites only — registration.py/payment.py/scheduler.py behavioral gates remain for 06-06). Not marked complete in REQUIREMENTS.md; will be marked once 06-07 confirms full-phase coverage.

# Metrics
duration: 11min
completed: 2026-07-24
---

# Phase 6 Plan 5: Admin Feature-switch Read Migration Summary

**The ⚙️ Настройки landing screen (render_settings_text), its toggle-button keyboard (build_settings_keyboard), and 4 admin toggle-handler read-sites now resolve their ~14 feature-switch values through `get_setting_typed` instead of the hardcoded `get_setting(k) or "<literal>"` idiom — byte-identical on screen, proven by a new automated snapshot test covering both default and mixed-value cases.**

## Performance

- **Duration:** 11 min
- **Started:** 2026-07-24T16:34:10+03:00 (approx, after prior plan's last commit)
- **Completed:** 2026-07-24T16:44:41+03:00
- **Tasks:** 2 completed
- **Files modified:** 2 (handlers/admin.py, tests/test_settings_groups_c0x.py)

## Accomplishments

- `render_settings_text`: 10 feature-switch reads (`reg_bonus_enabled`, `full_approval`, `short_approval`, `pending_notify_mode`, `payment_enabled`, `consent_enabled`, `payment_reminders_enabled`, `party_enabled`, `party_fork_question`, `party_approval`) migrated from `await get_setting(k) or "<literal>"` to `await get_setting_typed(k)`
- `build_settings_keyboard`: 14 feature-switch reads migrated the same way (adds `registration_mode`, `reg_university_mode`, `edu_conditional`, `reg_show_progress` to the render_settings_text set) — every button-text ternary and callback_data string (`settings_toggle_reg`, `toggle_party_enabled`, `toggle_payment_enabled`, etc.) left byte-identical, per D-12
- 4 admin toggle-handler read-sites migrated: `toggle_registration_mode` (registration_mode), `toggle_notify_mode` (pending_notify_mode), `toggle_bonus` (reg_bonus_enabled), and `_refresh_party_sheet_header`'s party_enabled gate — only the current-value READ moved; every `set_setting` write path is unchanged
- New automated regression net: `test_settings_landing_text_snapshot` and `test_settings_toggle_button_snapshot` lock the exact rendered labels, button texts, and callback_data (with positions) for both the all-default (fresh DB) case and a mixed/non-default case — established as a PASS-first regression lock (not RED-first, since Task 2 preserves byte-for-byte output) and confirmed still green after Task 2's migration
- Full suite: 384/384 tests pass (29 in the plan's own test scope), plus a clean import smoke test (`python -c "import handlers.admin, main"`)

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing snapshot test for settings landing text + toggle-button block** - `2f68ea9` (test) — established the regression lock; both new tests PASS against the pre-migration code (per plan's own guidance: full byte-for-byte preservation means a RED-first test would only fail if written incorrectly)
2. **Task 2: Migrate admin feature-switch reads to get_setting_typed** - `5065c13` (feat) — GREEN, all 29 tests in the plan's scope pass, full suite 384/384

**Plan metadata:** (this commit, following this Summary)

_Note: Task 1 was tagged `tdd="true"` at the plan level but the plan's own action text explicitly directs writing the tests to PASS against current code (regression-lock pattern) rather than RED-first, because the migration is byte-for-byte preserving by design — a RED-first test here would only fail due to an authoring mistake, not due to missing functionality. No REFACTOR commit was needed._

## Files Created/Modified

- `handlers/admin.py` - `render_settings_text` (10 reads), `build_settings_keyboard` (14 reads), and 4 toggle-handler current-value reads (`toggle_registration_mode`, `toggle_notify_mode`, `toggle_bonus`, `_refresh_party_sheet_header`'s party_enabled gate) now call `get_setting_typed(key)` instead of `get_setting(key) or "<literal>"`; button structure, ternaries, and callback_data strings untouched
- `tests/test_settings_groups_c0x.py` - added `test_settings_landing_text_snapshot` and `test_settings_toggle_button_snapshot`, covering the all-default case and a mixed/non-default case for both the landing text and the toggle-button keyboard (exact text + callback_data + position)

## Decisions Made

- **D-12 honored, no button-generation refactor:** the bespoke per-toggle callback_data strings and text ternaries in `build_settings_keyboard` were left exactly as they were — only the preceding `await get_setting(...)` calls were swapped for `await get_setting_typed(...)`. Verified by grep confirming `settings_toggle_reg`, `toggle_party_enabled`, `toggle_payment_enabled` are unchanged and by the new snapshot test asserting their exact positions.
- **render_settings_text's own `registration_mode` read intentionally left unmigrated:** the plan's `must_haves.truths` bullet and `interfaces` block both explicitly enumerate the render_settings_text feature-switch keys to migrate, and `registration_mode` is not among them (only `build_settings_keyboard`'s registration_mode read at the analogous position is listed). Since this omission appears consistently in two independent parts of the plan (not a one-off typo), it was treated as an intentional scope boundary rather than a Rule-1 bug to auto-fix — migrating it would have been unrequested scope expansion beyond the plan's explicit interfaces list. The acceptance-criteria grep for the `or "<literal>"` idiom is satisfied because that criterion targets "the migrated feature-switch reads," and this read was never in the migrated set.
- **Generic multi-key toggle helpers left untouched:** `_toggle_module_setting`, `_toggle_approval_setting`, and `_toggle_value_setting` (shared handlers parameterized by a `key` argument, used by `payment_enabled`/`consent_enabled`/`party_enabled`/`party_fork_question`/`reg_university_mode`/`edu_conditional`/`reg_show_progress` toggles) still read via `get_setting(key) or default`. The plan's interfaces block names exactly 4 single-key handler read-sites for this wave (`registration_mode`, `pending_notify_mode`, `reg_bonus_enabled`, the party_enabled gate) — these generic helpers were not in that list, so they were left as-is to respect the plan's stated scope boundary.

## Deviations from Plan

None - plan executed exactly as written. Line numbers in the plan's interfaces block had drifted slightly from the actual current source (e.g., `render_settings_text` starts at line 463 not 465, the party_enabled gate is at line 2234 not 2229) due to intervening edits from prior plans in this phase, but the named functions/keys/read-sites were located and verified against the actual source before migrating — no ambiguity in what to migrate.

## Issues Encountered

None. `python -c "import handlers.admin, main"` confirmed no circular import. Full suite (384/384) green after the migration, including the two new snapshot tests and all pre-existing registration/payment/admin regression tests.

## User Setup Required

None - no external service configuration required. No `bot_settings` DB migration needed (registry reads existing rows via the unchanged `get_setting`; no schema change, no live delegate's currently-set toggle values are affected — only the metadata/default source moved).

## Next Phase Readiness

- REG-02 (partial): the admin-side feature-switch READ consumer wiring for `render_settings_text`, `build_settings_keyboard`, and the 4 named toggle-handler sites is complete and byte-for-byte verified. The remaining feature-switch CONSUMER read-sites in `handlers/registration.py`, `handlers/payment.py`, and `services/scheduler.py` (the behavioral gates, not the admin display) are deferred to 06-06 as planned.
- The `_toggle_module_setting`/`_toggle_approval_setting`/`_toggle_value_setting` generic handlers and `render_settings_text`'s registration_mode read remain on the old `get_setting(k) or default` idiom — these were out of this wave's explicit scope; 06-07 (final coverage plan) should confirm whether they need a follow-up pass or are intentionally left as a residual literal-idiom island.
- No blockers. Live delegates' current admin-configured toggle settings were never touched at the data level — only the metadata/default source changed; the byte-for-byte snapshot tests and the full existing regression suite confirm zero behavior drift.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: handlers/admin.py (get_setting_typed migrated reads present)
- FOUND: 2f68ea9 (test commit)
- FOUND: 5065c13 (feat commit)
