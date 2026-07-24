---
phase: 06-settings-schema-registry
plan: 04
subsystem: config
tags: [settings-registry, admin-ui, aiogram3, toggle, enum, feature-switches]

# Dependency graph
requires:
  - phase: 06-settings-schema-registry
    provides: "settings_schema.py registry module + _parse_setting/get_setting_typed (06-01), reg/pay/party/consent groups (06-02), migrated consumers (06-03)"
provides:
  - "SETTINGS_SCHEMA reg_questions group: all 43 reg_q_* keys as type:toggle entries (default on/off byte-for-byte from REG_DEFAULTS)"
  - "SETTINGS_SCHEMA toggles group: 14 feature-switch keys as type:enum entries with verified or-idiom defaults, ready for 06-05/06-06 consumer wiring"
  - "handlers/registration.py::REG_DEFAULTS derived (comprehension over SETTINGS_SCHEMA toggle entries), name retained"
  - "handlers/admin.py's 3 duplicated reg_q on/off read-sites (render_settings_text loop, _is_question_on, toggle_reg_question) resolve through get_setting_typed"
affects: [06-05, 06-06, 06-07]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "REG_DEFAULTS as a computed re-export: name/shape preserved for legacy consumers (admin.py import + preset bulk-write loop), value now sourced from a single registry filter"
    - "Toggle vs enum typing split for on/off keys: reg_q_* -> type:toggle (bool via (raw=='on') if raw is not None else (default=='on')); feature-switches -> type:enum (string via raw if raw else default) so the live `get_setting(k) or '<default>'` idiom stays byte-identical for downstream `!= 'on'`/`== 'on'` string comparisons"
    - "Collapsing a fetch-then-compare read-site into get_setting_typed(key) when the raw value isn't reused elsewhere in the function (removes duplicate parse logic without adding a duplicate DB call)"

key-files:
  created: []
  modified: [settings_schema.py, handlers/registration.py, handlers/admin.py, tests/test_settings_groups_c0x.py]

key-decisions:
  - "D-06 (06-04): REG_DEFAULTS = {k: v['default'] for k, v in SETTINGS_SCHEMA.items() if v['type'] == 'toggle'} — a comprehension, not a literal; test_reg_defaults_parity pins it byte-for-byte against a frozen 43-key oracle copied from the pre-migration source"
  - "T-06-14 resolved: handlers/registration.py imports settings_schema.SETTINGS_SCHEMA at module top with zero cycle risk (settings_schema imports only database.db); confirmed via `python -c \"import settings_schema, handlers.registration, handlers.admin, main\"`"
  - "Toggle labels for the 43 reg_q_* registry entries are COPIED (not imported) from handlers/registration.py::REG_LABELS into settings_schema.py, because importing REG_LABELS the other way would create the exact cycle T-06-14 warns against (registration.py already imports settings_schema); verified byte-for-byte via a direct dict-value comparison script, zero mismatches"
  - "Deviation (Rule 1): 06-04-PLAN.md's interfaces table describes REG_DEFAULTS as \"44 keys\" but the actual pre-migration literal (handlers/registration.py:197-241) has 43 unique keys (verified by direct source count: `block.count(':')` == 43). The frozen oracle in the tests, and the registry's reg_questions group, both pin the source-verified count (43), per the plan's own acceptance criterion \"matches registration.py:197-241 exactly\" — the ground truth is the source, not the plan's stated count."
  - "The two read-sites that previously fetched a raw value and only used it for the toggle comparison (render_settings_text's REG_FLOW loop, toggle_reg_question) were collapsed to a single `await get_setting_typed(sk)` call — same one get_setting DB read as before, no duplicate I/O, less code duplication than an intermediate SETTINGS_SCHEMA[sk]['default'] lookup would have required"

patterns-established:
  - "Frozen-oracle regression test pattern: when a migration changes WHERE a default value comes from, pin the OLD value as an inline literal in the test file (independent of the new derivation) so the test can't be fooled by a bug in the derivation itself"

requirements-completed: []  # REG-01/REG-02 are phase-wide (span 06-01..06-07); this plan is a partial contribution (toggle+enum registry entries + reg_q read-site migration only — feature-switch CONSUMER wiring is 06-05/06-06). Not marked complete in REQUIREMENTS.md; will be marked once 06-07 confirms full-phase coverage.

# Metrics
duration: 11min
completed: 2026-07-24
---

# Phase 6 Plan 4: Toggle Wave — reg_q Toggles + Feature-switch Enums Summary

**All 43 `reg_q_*` question toggles and all 14 feature-switch keys are now described in `SETTINGS_SCHEMA` (reg_questions/toggles groups); `REG_DEFAULTS` is a computed re-export instead of a hand-maintained literal, and the three duplicated `reg_q` on/off read-sites in `handlers/admin.py` resolve their default through the registry, byte-for-byte.**

## Performance

- **Duration:** 11 min
- **Started:** 2026-07-24T16:17:40+03:00 (approx, first commit 16:20:47+03:00)
- **Completed:** 2026-07-24T16:28:27+03:00
- **Tasks:** 2 completed
- **Files modified:** 4 (settings_schema.py, handlers/registration.py, handlers/admin.py, tests/test_settings_groups_c0x.py)

## Accomplishments

- `settings_schema.py`: added a `reg_questions` group with all 43 `reg_q_*` keys as `type: toggle` entries — labels copied byte-for-byte from `REG_LABELS`, defaults copied byte-for-byte from the pre-migration `REG_DEFAULTS` literal
- `settings_schema.py`: added a `toggles` group with all 14 feature-switch keys (`party_enabled`, `party_fork_question`, `reg_bonus_enabled`, `payment_enabled`, `consent_enabled`, `payment_reminders_enabled`, `edu_conditional`, `reg_show_progress`, `reg_university_mode`, `registration_mode`, `pending_notify_mode`, `full_approval`, `short_approval`, `party_approval`) as `type: enum` entries with defaults verified byte-for-byte from the live call sites (not guessed) — ready for their CONSUMER read-sites to be wired in 06-05 (admin) and 06-06 (registration/payment/scheduler)
- `handlers/registration.py::REG_DEFAULTS` is now a one-line comprehension over `SETTINGS_SCHEMA`, not a 44-line literal — the name is retained (admin.py still imports and iterates it) but its value is derived
- `handlers/admin.py`'s three duplicated `REG_DEFAULTS.get(key, "on") == "on"` reg_q read-sites (the `render_settings_text` REG_FLOW loop, `_is_question_on`, `toggle_reg_question`) now resolve via `get_setting_typed(key)` — byte-identical output, no duplicate DB reads introduced
- Full-registry coverage test (D-17/WARNING-2) added: iterates every `SETTINGS_SCHEMA` entry unconditionally, catching drift across all 8 types at once
- Full suite: 382/382 tests pass (104 in the plan's own test scope, 382 project-wide), plus a clean import-cycle check (`settings_schema, handlers.registration, handlers.admin, main`)

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing toggle parse-equivalence + REG_DEFAULTS-parity + full-registry coverage tests** - `bba466c` (test) — RED confirmed: 3/5 new tests failed because the toggle+enum keys were not yet registered
2. **Task 2: Add toggle+enum entries to the registry, derive REG_DEFAULTS, migrate the three reg_q read-sites** - `2c9dacd` (feat) — GREEN, all 104 tests in the plan's scope pass, full suite 382/382

**Plan metadata:** (this commit, following this Summary)

_Note: Task 1/2 followed the TDD RED→GREEN cycle (tdd="true" plan-level gate); no REFACTOR commit was needed._

## Files Created/Modified

- `settings_schema.py` - added `reg_questions` group (43 toggle entries) and `toggles` group (14 enum entries) to `SETTINGS_SCHEMA`; no changes to `_parse_setting`/`get_setting_typed` (reused the existing toggle/enum branches verbatim, per plan instruction to write no new parse code)
- `handlers/registration.py` - `REG_DEFAULTS` replaced with a comprehension over `SETTINGS_SCHEMA`; added `from settings_schema import SETTINGS_SCHEMA` import at module top (no cycle — settings_schema depends only on `database.db`)
- `handlers/admin.py` - imports `get_setting_typed` alongside `SETTINGS_SCHEMA`; the three reg_q toggle read-sites (`render_settings_text`'s enabled-question counter, `_is_question_on`, `toggle_reg_question`) now call `get_setting_typed(key)` instead of the manual `REG_DEFAULTS.get(...)` fallback
- `tests/test_settings_groups_c0x.py` - added `test_toggle_parse_equivalence_all_keys`, `test_reg_defaults_parity`, `test_toggle_keys_coverage`, `test_enum_feature_switch_defaults`, `test_full_registry_coverage`; widened `test_registry_coverage_event`'s `allowed_groups` to include the new `"toggles"` group

## Decisions Made

- **Toggle typing for reg_q_* keys:** `type: toggle`, bool via `(raw == "on") if raw is not None else (default == "on")` — matches `_is_question_on`'s exact idiom, reused verbatim from `_parse_setting`'s existing toggle branch (06-01).
- **Enum typing for feature-switch keys (not toggle):** these keys are read downstream via string comparisons (`!= "on"`, `== "manual"`, etc.), not consumed as Python bools — `type: enum` preserves the exact resolved STRING via the `raw if raw else default` falsy-to-default branch (D-15), matching the live `get_setting(k) or "<default>"` idiom byte-for-byte including empty-string. Confirmed by `test_enum_feature_switch_defaults` asserting `_parse_setting(key, "") == default` for all 14 keys.
- **Labels copied, not imported:** `settings_schema.py` cannot import `REG_LABELS` from `handlers.registration` (that would create the exact import cycle T-06-14 exists to prevent, since `registration.py` now imports `settings_schema`). Labels for the 43 toggle entries are literal copies, verified byte-identical via a direct dict-comparison script (zero mismatches across all 43 keys).
- **43 keys, not 44 (deviation, Rule 1):** the plan's interfaces table states "44 keys" for the REG_DEFAULTS toggle set, but the actual pre-migration source (`handlers/registration.py:197-241`) has exactly 43 unique keys — verified by direct count. The frozen test oracle and registry entries both pin 43 (the source-verified ground truth), per the plan's own acceptance criterion that the oracle must match the source "exactly."
- **Collapsed fetch-then-compare sites to a single `get_setting_typed` call:** two of the three read-sites (`render_settings_text`'s loop, `toggle_reg_question`) previously fetched the raw value into a local variable used ONLY for the toggle comparison; replacing both the fetch and the comparison with one `await get_setting_typed(sk)` call keeps the DB I/O count identical (one `get_setting` call each) while removing the duplicated parse logic.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Plan's "44-key" REG_DEFAULTS count corrected to the source-verified 43**
- **Found during:** Task 1 (writing the frozen oracle map)
- **Issue:** 06-04-PLAN.md's interfaces section states the toggle key list has "44 keys," but copying the literal from `handlers/registration.py:197-241` and counting programmatically (`block.count(':')`) yields 43. A sanity assertion pinned to 44 fails collection immediately.
- **Fix:** Pinned the sanity assertion (and all downstream logic) to 43, the value verified directly from the live source file — matches the plan's own acceptance criterion that the oracle "matches registration.py:197-241 exactly."
- **Files modified:** tests/test_settings_groups_c0x.py
- **Verification:** `python -c "..."` count script confirms 43; `test_reg_defaults_parity` (comparing the derived REG_DEFAULTS against the 43-key oracle) passes.
- **Committed in:** bba466c (Task 1 commit)

**2. [Rule 1 - Bug] test_registry_coverage_event's allowed_groups set updated for the new "toggles" group**
- **Found during:** Task 2, first full-suite run after adding the toggles group
- **Issue:** The pre-existing (06-01) `test_registry_coverage_event` hardcodes an `allowed_groups` set that did not include `"toggles"` — every new feature-switch entry failed this coverage assertion.
- **Fix:** Added `"toggles"` to the `allowed_groups` set with a one-line comment noting when/why it was added.
- **Files modified:** tests/test_settings_groups_c0x.py
- **Verification:** `python -m pytest tests/test_settings_groups_c0x.py -q` — 104/104 pass after the fix.
- **Committed in:** 2c9dacd (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs — both test-scope corrections, no production-logic changes beyond the plan's described scope)
**Impact on plan:** Both fixes were required for the plan's own acceptance criteria to be satisfiable; no scope creep — feature-switch consumer wiring remains untouched, deferred to 06-05/06-06 as specified.

## Issues Encountered

None beyond the two deviations above. `python -c "import settings_schema, handlers.registration, handlers.admin, main"` confirmed no circular import after `registration.py` started importing `settings_schema` — settings_schema's zero-upstream-dependency design (D-01) held exactly as designed.

## User Setup Required

None - no external service configuration required. No `bot_settings` DB migration needed (registry reads existing rows via the unchanged `get_setting`; no schema change, no live delegate's currently-set toggle values are affected).

## Next Phase Readiness

- REG-01: the registry now describes every `reg_q_*` toggle AND every feature-switch enum — "one file instead of four" holds for the toggle/enum layer, matching the North Star for this key category.
- REG-02 (partial): reg_q on/off resolution flows through the registry byte-for-byte at all 3 known duplicated read-sites; the 14 feature-switch entries are registered with verified defaults and are ready for their CONSUMER call sites (admin.py's remaining `get_setting("party_enabled") or "off"`-style reads, registration.py/payment.py/scheduler.py's feature-switch checks) to be migrated in 06-05/06-06 without re-deriving or re-verifying any default.
- No blockers. Live delegates' current toggle/feature-switch settings were never touched at the data level — only the metadata source changed; the byte-for-byte parity tests (`test_toggle_parse_equivalence_all_keys`, `test_reg_defaults_parity`, `test_enum_feature_switch_defaults`) and the full existing regression suite (`test_registration_phase4`/`test_registration_phase5`) confirm zero behavior drift.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## TDD Gate Compliance

Gate sequence confirmed in git log for this plan:
1. RED gate: `bba466c test(06-04): add failing toggle parity + enum-default + full-registry coverage tests` — confirmed failing (3/5 new tests failed, keys not yet registered) before commit.
2. GREEN gate: `2c9dacd feat(06-04): register toggle+enum entries, derive REG_DEFAULTS, migrate reg_q read-sites` — confirmed passing (104/104 plan-scope tests, 382/382 full suite) after commit.

No REFACTOR commit was needed — no code smell/duplication surfaced after GREEN.

## Self-Check: PASSED

- FOUND: settings_schema.py
- FOUND: bba466c (test commit)
- FOUND: 2c9dacd (feat commit)
- FOUND: a56b2ea (docs/summary commit)
