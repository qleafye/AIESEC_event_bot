---
phase: 06-settings-schema-registry
plan: 03
subsystem: config
tags: [settings-registry, consumers, aiogram3, tdd]

# Dependency graph
requires: ["06-01", "06-02"]
provides:
  - "services/reminders.py::pending_reminder_loop reads pending_reminder_interval via settings_schema.get_setting_typed"
  - "services/scheduler.py::sweep_payment_overdue reads payment_deadline via settings_schema.get_setting_typed"
  - "keyboards/builders.py::get_source_kb reads source_options via settings_schema.get_setting_typed"
  - "tests/test_settings_consumers_phase6.py (5 tests: 3 oracle-equivalence, 2 consumer-wiring)"
affects: [06-04, 06-05, 06-06, 06-07]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Consumer migration recipe: add module-level `from settings_schema import get_setting_typed` import, replace the single hand-rolled parse call site, keep the old pure-helper function in place (retagged as the parse oracle) rather than deleting it — existing tests and this plan's own oracle-equivalence tests still reference it"
    - "Module-attribute monkeypatch for infinite-loop consumer tests: patch `<module>.get_setting_typed` + a StopLoop-raising asyncio.sleep to run exactly one loop iteration and assert the registry accessor was actually invoked, without restructuring the production loop"

key-files:
  created: [tests/test_settings_consumers_phase6.py]
  modified: [services/reminders.py, services/scheduler.py, keyboards/builders.py]

key-decisions:
  - "T-06-09 (locked): consumer-side oracle equivalence proven at the CALL-SITE level (not just the registry's own parse-equivalence tests from 06-02) — pending_reminder_interval/payment_deadline/source_options each got a dedicated test comparing get_setting_typed's resolved value to the pre-migration oracle over a real tmp DB across None/empty/garbage/valid inputs"
  - "Only payment_deadline migrated in services/scheduler.py — nudge_scan_minutes, allowlist_refresh_minutes, incomplete_sync_hours, nudge_after_minutes are int settings NOT yet in SETTINGS_SCHEMA (06-02 only added pending_reminder_interval/payment_deadline as the int/date entries), so per the plan's explicit instruction they stay on `_int_or_default` untouched — no scope bleed into settings not yet registry-migrated"
  - "pending_reminder_enabled (toggle) explicitly left on the old get_setting + _reminder_enabled path — toggle absorption is 06-04's wave, confirmed unchanged by grep in acceptance criteria"
  - "Pure parse-oracle helpers (_reminder_interval, _parse_schedule_dt) kept in place rather than deleted, each with a retagged docstring note explaining they're no longer called by the production path but remain the test oracle for tests/test_reminders_phase2.py, tests/test_settings_groups_c0x.py, and this plan's new test file"
  - "get_source_kb keeps its own `if not items: items = DEFAULT_SOURCE_OPTIONS` guard around the typed read rather than relying on the registry's own list-type empty fallback, because the registry's `default` for source_options is None (not DEFAULT_SOURCE_OPTIONS) — preserves T-06-11's exact empty->DEFAULT_SOURCE_OPTIONS behavior"

patterns-established:
  - "Consumer-wiring test pattern (module-attribute monkeypatch + StopLoop sentinel) reusable for any future infinite-loop consumer migration (e.g. services/scheduler.py's other interval jobs, once their settings join the registry)"

requirements-completed: []  # REG-02 spans all of phase 6's consumer waves; toggle consumers (06-04) haven't migrated yet, so REG-02 is not fully closed until 06-04+ finish. Not marked complete here — same rationale as 06-01/06-02 SUMMARY.

# Metrics
duration: 14min
completed: 2026-07-24
---

# Phase 6 Plan 3: Migrate Value Consumers to get_setting_typed Summary

**Switched the three non-toggle value consumers (services/reminders.py, services/scheduler.py, keyboards/builders.py) to read pending_reminder_interval/payment_deadline/source_options through `settings_schema.get_setting_typed` instead of their own hand-rolled parse, with byte-for-byte behavior proven via dedicated oracle-equivalence + consumer-wiring tests — zero behavior change for the ~590 live delegates.**

## Performance

- **Duration:** 14 min
- **Completed:** 2026-07-24
- **Tasks:** 2 completed
- **Files modified:** 4 (1 created: tests/test_settings_consumers_phase6.py; 3 modified: services/reminders.py, services/scheduler.py, keyboards/builders.py)

## Accomplishments

- `tests/test_settings_consumers_phase6.py` created (new file, per D-17-style incremental-migration convention, avoiding write-conflict with `tests/test_settings_groups_c0x.py`): 5 tests — 3 oracle-equivalence (int/date/list, matching `_reminder_interval`/`_parse_schedule_dt`/the splitlines idiom over None/empty/garbage/valid inputs against a real tmp DB) + 2 consumer-wiring tests (monkeypatch `get_setting_typed` on the consumer module + a `StopLoop`-raising `asyncio.sleep` to run `pending_reminder_loop` for exactly one iteration and assert the registry accessor was actually invoked; same pattern applied to `get_source_kb`)
- RED confirmed pre-migration: oracle-equivalence tests passed immediately (06-02's registry entries are already correct), the 2 consumer-wiring tests failed with empty `calls` lists (consumers still on the old parse path) — exactly the RED/GREEN split the plan called for
- `services/reminders.py::pending_reminder_loop` now resolves `pending_reminder_interval` via `await get_setting_typed("pending_reminder_interval")`; `pending_reminder_enabled` (toggle) left untouched per D-12 deferral
- `services/scheduler.py::sweep_payment_overdue` now resolves `payment_deadline` via `await get_setting_typed("payment_deadline")`, collapsing the old `get_setting` + manual `datetime.strptime` + `ValueError` guard into one call with identical None-on-bad-input semantics; the other four scheduler int settings (nudge_scan_minutes, allowlist_refresh_minutes, incomplete_sync_hours, nudge_after_minutes) are NOT in `SETTINGS_SCHEMA` yet and were correctly left on `_int_or_default` per the plan's explicit "migrate ONLY registry-migrated keys" instruction
- `keyboards/builders.py::get_source_kb` now resolves `source_options` via `await get_setting_typed("source_options")`, keeping the `if not items: items = DEFAULT_SOURCE_OPTIONS` guard around it since the registry's list default is `None` (not `DEFAULT_SOURCE_OPTIONS`) — preserves the exact empty→default fallback (T-06-11)
- No import cycle: `python -c "import services.reminders, services.scheduler, keyboards.builders, main"` exits 0 — `settings_schema.py`'s zero-upstream-dependency design (D-01) held with a plain module-level import at all three sites, no lazy-import workaround needed
- Full regression: `tests/test_settings_consumers_phase6.py` 5/5 green; full project suite 377/377 green (372 prior + 5 new)

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing consumer behavior-preservation tests** - `bc54533` (test) — RED confirmed: 3 oracle-equivalence tests pass (registry from 06-02 correct), 2 consumer-wiring tests fail (`assert 'pending_reminder_interval' in []` / `assert 'source_options' in []`)
2. **Task 2: Migrate reminders/scheduler/builders to get_setting_typed** - `810923a` (feat) — GREEN, all 5 tests in the file pass, full suite 377/377, no import cycle

_Note: Task 1 followed the TDD RED→GREEN cycle (tdd="true"); Task 2 was a plain auto task that flipped the RED consumer-wiring assertions to GREEN through the migration._

## Files Created/Modified

- `tests/test_settings_consumers_phase6.py` - new file: `test_reminders_interval_via_registry_matches_oracle`, `test_scheduler_date_via_registry_matches_oracle`, `test_source_kb_unchanged`, `test_reminders_loop_reads_interval_via_registry`, `test_source_kb_reads_via_registry`
- `services/reminders.py` - added `from settings_schema import get_setting_typed`; `pending_reminder_loop`'s interval resolution switched to `await get_setting_typed("pending_reminder_interval")`; `_reminder_interval` retained (retagged as parse-oracle for tests)
- `services/scheduler.py` - added `from settings_schema import get_setting_typed`; `sweep_payment_overdue`'s `payment_deadline` resolution switched to `await get_setting_typed("payment_deadline")`; `_parse_schedule_dt` retained (retagged as parse-oracle for tests)
- `keyboards/builders.py` - added `from settings_schema import get_setting_typed`; `get_source_kb`'s `source_options` resolution switched to `await get_setting_typed("source_options")`, empty→`DEFAULT_SOURCE_OPTIONS` guard preserved

## Decisions Made

- **T-06-09 consumer-level oracle equivalence:** Rather than relying solely on 06-02's registry-internal parse-equivalence tests, this plan added call-site-level oracle tests that exercise the actual consumer function against a real tmp DB, closing the gap between "the registry parses correctly in isolation" and "the consumer actually gets the same value at runtime."
- **Scoped scheduler migration:** Only `payment_deadline` was migrated in `services/scheduler.py`. The four other int settings it reads (`nudge_scan_minutes`, `allowlist_refresh_minutes`, `incomplete_sync_hours`, `nudge_after_minutes`) are not yet `SETTINGS_SCHEMA` entries — migrating them now would have been out of scope (06-02 never added them) and risked masking a missing-registry-entry bug as a successful migration.
- **Toggle deferral honored:** `pending_reminder_enabled` in `services/reminders.py` was explicitly left calling `get_setting` + `_reminder_enabled` — confirmed via the plan's own grep-based acceptance criterion, no scope bleed into the 06-04 toggle wave.
- **Retained pure-helper functions as test oracles:** `_reminder_interval` and `_parse_schedule_dt` are no longer called by production code but were kept (not deleted) because three other test files (`tests/test_reminders_phase2.py`, `tests/test_settings_groups_c0x.py`, and this plan's own file) import and call them directly as the equivalence oracle. Deleting them would have broken those tests for no behavioral gain.

## Deviations from Plan

None — plan executed exactly as written. Task 1 produced the expected RED split (oracle-equivalence green, consumer-wiring red); Task 2 produced the expected GREEN with zero import-cycle risk materializing (D-01's zero-upstream-dependency design for `settings_schema.py` held, so the lazy-import fallback mentioned in the plan/threat model was never needed).

## TDD Gate Compliance

Gate sequence confirmed in git log for this plan:
1. RED gate: `bc54533 test(06-03): add failing consumer parse-equivalence + wiring tests` — confirmed failing (2 of 5 tests: consumer-wiring for reminders + source_kb) before the commit.
2. GREEN gate: `810923a feat(06-03): migrate reminders/scheduler/builders to get_setting_typed` — confirmed passing (all 5 tests in the file, plus full 377-test suite) after the commit.

No REFACTOR commit was needed — no code smell/duplication surfaced after GREEN; each migrated call site was a single-line swap plus a provenance comment.

## Issues Encountered

None. `python -c "import services.reminders, services.scheduler, keyboards.builders, main"` confirmed no circular import — `settings_schema.py`'s one-directional dependency on `database.db` only (D-01) held across all three new import sites, matching the threat model's T-06-10 expectation.

## User Setup Required

None — no external service configuration required. No `bot_settings` DB migration needed; the registry reads existing rows via the unchanged `get_setting`, and no schema change occurred. All ~590 live users' reminder cadence / payment deadline / source-options behavior is unchanged, proven by the oracle-equivalence tests against a real tmp DB.

## Next Phase Readiness

- REG-02's non-toggle consumer migration is now complete for all three targeted files (reminders/scheduler/builders) — the remaining REG-02 surface is the toggle-reading call sites (`handlers/admin.py`'s `is_on`/`_is_question_on` idiom, `REG_DEFAULTS`-based reads), explicitly deferred to 06-04 per D-12.
- The consumer-wiring test pattern (module-attribute monkeypatch + `StopLoop`-raising `asyncio.sleep`) established here is reusable for 06-04's toggle consumer tests and any future infinite-loop consumer migration in `services/scheduler.py`'s other interval jobs, once their settings join the registry.
- No blockers.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: tests/test_settings_consumers_phase6.py
- FOUND: bc54533 (test commit)
- FOUND: 810923a (feat commit)
