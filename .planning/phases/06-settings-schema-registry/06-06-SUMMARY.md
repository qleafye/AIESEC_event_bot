---
phase: 06-settings-schema-registry
plan: 06
subsystem: config
tags: [settings-registry, aiogram3, feature-switches, approval-flow, payments, scheduler]

# Dependency graph
requires:
  - phase: 06-settings-schema-registry
    provides: "settings_schema.py registry + get_setting_typed accessor (06-01); toggle/enum registry entries with verified defaults (06-04); admin.py consumer wiring (06-05)"
provides:
  - "Every behavioral feature-switch read in handlers/registration.py, handlers/payment.py, services/scheduler.py resolves through settings_schema.get_setting_typed, byte-for-byte"
  - "_is_module_enabled(key) helper (consent_enabled/payment_enabled module gate) reads through the registry — covers all 4 call sites at once"
  - "_decide_status's caller (finalize_registration) feeds registration_mode/full_approval/short_approval/party_approval through get_setting_typed, including the previously-unmigrated full_approval key"
  - "Both documented RAW-read sites (registration_mode in process_full_name, party_approval feeding _decide_status) migrated after proving the registry default is interchangeable with the site's own None-handling"
affects: [06-07]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "RAW-read migration proof pattern: before migrating a read-site with no inline `or default`, verify the downstream branch treats the registry's enum default identically to what it does with raw None/absent — pin the proof with a dedicated equivalence test (test_raw_read_sites_preserved) rather than guessing"
    - "Source-inspection test (inspect.getsource) as a RED/GREEN wiring gate for read-sites embedded inside large, side-effecting functions (finalize_registration) that are impractical to drive end-to-end in tests — mirrors the existing '_finalize_row slice, don't drive the whole function' test style already used by tests/test_registration_phase5.py"

key-files:
  created: []
  modified: [handlers/registration.py, handlers/payment.py, services/scheduler.py, tests/test_settings_consumers_phase6.py]

key-decisions:
  - "BLOCKER-1 resolved: full_approval (finalize_registration -> _decide_status) migrated to get_setting_typed alongside short_approval/registration_mode — the plan's must_haves flagged this as previously missing from the toggle/consumer migration."
  - "BLOCKER-2 resolved: _is_module_enabled's body (not just call sites) migrated to `return await get_setting_typed(key) == \"on\"` — covers consent_enabled/payment_enabled at all 4 call sites (registration.py:483/520/2320/2230-ish, exact lines drift-verified) in one change."
  - "RAW-read site 1 (process_full_name's bare `mode = await get_setting(\"registration_mode\")`, branch `!= \"full\"`) migrated to get_setting_typed: registry default \"short\" != \"full\", so None/\"\" fail the branch identically before and after — proven by test_raw_read_sites_preserved over {None, \"\", \"full\", \"short\"}."
  - "RAW-read site 2 (finalize_registration's bare `party_setting = await get_setting(\"party_approval\")`, fed into _decide_status's own `party_setting or \"manual\"` fallback) migrated to get_setting_typed: registry default \"manual\" is byte-identical to that fallback, so passing the resolved value instead of raw/None cannot change the produced status — proven via `_decide_status` called with both the raw and typed value across {None, \"\", \"manual\", \"auto\"}."
  - "_is_step_enabled (registration.py:373-377) and its REG_DEFAULTS-based self-healing were deliberately left untouched (06-04 already made REG_DEFAULTS a derived re-export) — only _is_module_enabled hardcoded its own default and needed wiring, per the plan's explicit instruction."
  - "The log-only registration_mode read inside finalize_registration's f-string (`f\"mode={await get_setting('registration_mode') or 'short'} ...\"`, not one of the plan's cited interfaces sites) was deliberately left untouched — cosmetic logging only, not a behavioral gate, out of the plan's explicit migration list."

patterns-established:
  - "Wiring-verification via monkeypatch: for gates wrapped in small standalone helpers (_should_show_fork, _progress, _get_enabled_steps, _is_module_enabled, should_offer_receipt_upload, send_payment_reminder), tests monkeypatch the target module's `get_setting_typed` name and assert the expected key(s) were invoked — this is what makes the RED phase meaningful, since value-equivalence alone can't fail (D-15 guarantees the registry default IS the old idiom's default)."

requirements-completed: []  # REG-02 is phase-wide (spans 06-01..06-07); this plan completes the reg/payment/scheduler consumer wave specifically. Not marked complete in REQUIREMENTS.md until 06-07 confirms full-phase coverage.

# Metrics
duration: 22min
completed: 2026-07-24
---

# Phase 6 Plan 6: Reg/Payment/Scheduler Feature-switch Gate Migration Summary

**Every behavioral feature-switch gate in handlers/registration.py, handlers/payment.py, and services/scheduler.py — including the previously-unmigrated `full_approval` (BLOCKER-1) and the `_is_module_enabled` consent/payment module gate (BLOCKER-2) — now resolves through `settings_schema.get_setting_typed`, byte-for-byte, with both RAW-read sites migrated after an explicit branch-level equivalence proof.**

## Performance

- **Duration:** 22 min
- **Started:** 2026-07-24T16:47:00+03:00 (approx)
- **Completed:** 2026-07-24T17:04:30+03:00
- **Tasks:** 2 completed
- **Files modified:** 4 (handlers/registration.py, handlers/payment.py, services/scheduler.py, tests/test_settings_consumers_phase6.py)

## Accomplishments

- `handlers/registration.py`: migrated 12 inline feature-switch reads (`edu_conditional`, `reg_university_mode`, `party_fork_question`, `party_enabled` x3, `reg_show_progress`, `registration_mode` x2, `reg_bonus_enabled`, `full_approval`, `short_approval`, `party_approval`, `pending_notify_mode`) plus the `_is_module_enabled` helper body (covering `consent_enabled`/`payment_enabled` at all 4 call sites) — all now read through `get_setting_typed`
- `handlers/payment.py`: `should_offer_receipt_upload`'s `payment_enabled` gate migrated; new `from settings_schema import get_setting_typed` import added with zero cycle risk
- `services/scheduler.py`: both `payment_reminders_enabled` fire-time gates (`send_payment_reminder`, `sweep_payment_overdue`) migrated — `get_setting_typed` was already imported (06-03), no new import needed
- Both documented RAW-read sites (registration_mode's bare read in `process_full_name`; party_approval's bare read feeding `_decide_status`) were migrated to `get_setting_typed` after proving — not guessing — that the registry's enum default is provably interchangeable with each site's own None-handling
- 11 new tests added to `tests/test_settings_consumers_phase6.py`, combining real-DB value-equivalence matrices (covering None/""/on/off or the enum's own option set for every migrated key) with monkeypatch-based wiring checks and `inspect.getsource` source-checks for reads embedded in `finalize_registration` (too heavy to drive end-to-end safely)
- Full suite: 395/395 tests pass; plan-scope run (`test_settings_consumers_phase6.py` + `test_registration_phase4/5.py` + `test_payment_phase5.py`) 120/120; clean import smoke (`handlers.registration, handlers.payment, services.scheduler, main`)

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing gate behavior-preservation tests** - `23c2e55` (test) — RED confirmed: all 11 new tests failed (`python -m pytest tests/test_settings_consumers_phase6.py -k "gate_equiv or mode_equiv or is_module_enabled or reg_bonus or raw_read or approval" -q` → 11 failed, 5 deselected)
2. **Task 2: Migrate the reg/payment/scheduler feature-switch gates + _is_module_enabled** - `619df45` (feat) — GREEN, 120/120 plan-scope tests pass, full suite 395/395

**Plan metadata:** (this commit, following this Summary)

_Note: Task 1/2 followed the TDD RED→GREEN cycle (tdd="true" on Task 1); no REFACTOR commit was needed — Task 2 is a plain `auto` task per the plan's own task typing._

## Files Created/Modified

- `handlers/registration.py` - imports `get_setting_typed` alongside the existing `SETTINGS_SCHEMA` import; 12 inline reads + the `_is_module_enabled` helper body migrated from `get_setting(k) or "<default>"` / bare `== "on"` to `get_setting_typed(k)`, each tagged `# REG-02:`
- `handlers/payment.py` - new `from settings_schema import get_setting_typed` import; `should_offer_receipt_upload`'s `payment_enabled` gate migrated
- `services/scheduler.py` - both `payment_reminders_enabled` fire-time gates (already-imported `get_setting_typed`) migrated
- `tests/test_settings_consumers_phase6.py` - 11 new tests: `test_edu_conditional_gate_equiv`, `test_party_enabled_gate_equiv` (covers party_enabled/party_fork_question/reg_show_progress), `test_payment_enabled_gate_equiv`, `test_payment_reminders_gate_equiv` (covers both scheduler fire-time sites), `test_reg_bonus_enabled_equiv`, `test_is_module_enabled_gate_equiv`, `test_registration_mode_and_reg_university_mode_equiv`, `test_full_approval_gate_equiv`, `test_short_approval_and_party_approval_equiv`, `test_pending_notify_mode_gate_equiv`, `test_raw_read_sites_preserved`

## Decisions Made

- **BLOCKER-1 (full_approval):** migrated alongside short_approval/registration_mode into the same `finalize_registration` block that feeds `_decide_status` — the plan's must_haves explicitly flagged this key as at risk of being missed by prior waves.
- **BLOCKER-2 (_is_module_enabled):** migrated the HELPER BODY (`return await get_setting_typed(key) == "on"`) rather than each of its 4 call sites individually — one change covers `consent_enabled` and `payment_enabled` everywhere `_is_module_enabled` is called.
- **RAW-read site 1 (registration_mode, process_full_name):** migrated. Downstream branch is `mode != "full"`; registry default "short" fails that check identically to raw None/"" — proven over the full {None, "", "full", "short"} matrix in `test_raw_read_sites_preserved`.
- **RAW-read site 2 (party_approval, finalize_registration → _decide_status):** migrated. `_decide_status`'s own `party_setting or "manual"` fallback is byte-identical to the registry's enum default ("manual") — proven by calling `_decide_status` with both the raw and get_setting_typed-resolved value across {None, "", "manual", "auto"} and asserting the same status every time.
- **`_is_step_enabled` left untouched:** it already self-heals via 06-04's derived `REG_DEFAULTS` re-export; only `_is_module_enabled` hardcoded its own separate default and needed wiring, per the plan's explicit scope note.
- **Log-only registration_mode read left untouched:** `finalize_registration`'s logger.info f-string reads `get_setting('registration_mode') or 'short'` purely for a diagnostic log line — not one of the plan's cited interfaces sites, not a behavioral gate, and out of scope for this migration wave.

## Deviations from Plan

None — plan executed exactly as written, including both BLOCKER items and both RAW-read site decisions called out in the plan's interfaces/must_haves sections.

## Issues Encountered

None. `python -c "import handlers.registration, handlers.payment, services.scheduler, main"` confirmed no circular import after `handlers/payment.py` started importing `settings_schema` (settings_schema depends only on `database.db`; the dependency direction stayed one-way as designed in 06-01/D-01).

## User Setup Required

None - no external service configuration required. No `bot_settings` DB migration needed (registry reads existing rows via the unchanged `get_setting`; no schema change, no live delegate's currently-set toggle/feature-switch values are affected — confirmed by the byte-for-byte equivalence tests covering None/""/on/off for every migrated key).

## Next Phase Readiness

- REG-02 is now complete for the reg/payment/scheduler consumer wave: every behavioral feature-switch gate in these three files (including the two BLOCKER items and both RAW-read edge sites) reads through the registry, byte-for-byte, with per-key regression tests pinning the contract.
- Combined with 06-05 (admin.py consumer wiring), the only remaining REG-02 scope for 06-07 is a final full-phase coverage sweep (confirm no consumer anywhere still hand-rolls a `get_setting(...) or "<default>"` idiom for a key present in SETTINGS_SCHEMA) and marking REG-01/REG-02/REG-03 complete in REQUIREMENTS.md.
- No blockers. The ~590 live delegates' registration/approval/payment/reminder behavior is unchanged — every migrated site's byte-for-byte contract is proven by a dedicated equivalence test, and the full existing regression suite (395/395) plus the gated-flow suites (`test_registration_phase4/5.py`, `test_payment_phase5.py`) confirm zero behavior drift.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## TDD Gate Compliance

Gate sequence confirmed in git log for this plan:
1. RED gate: `23c2e55 test(06-06): add failing gate behavior-preservation tests for reg/pay/scheduler feature-switches` — confirmed failing (11/11 new tests failed under the `-k` filter) before commit.
2. GREEN gate: `619df45 feat(06-06): migrate reg/payment/scheduler feature-switch gates to get_setting_typed` — confirmed passing (120/120 plan-scope tests, 395/395 full suite) after commit.

No REFACTOR commit was needed — no code smell/duplication surfaced after GREEN (Task 2 was a plain `auto` task, not itself TDD-gated).

## Self-Check: PASSED

- FOUND: handlers/registration.py
- FOUND: handlers/payment.py
- FOUND: services/scheduler.py
- FOUND: 23c2e55 (test commit)
- FOUND: 619df45 (feat commit)
