---
phase: 06-settings-schema-registry
plan: 01
subsystem: config
tags: [settings-registry, admin-ui, aiogram3, type-dispatch]

# Dependency graph
requires: []
provides:
  - "settings_schema.py module with SETTINGS_SCHEMA dict-by-key registry"
  - "_parse_setting(key, raw) pure sync type-dispatch (text/enum/photo/file/int/date/list/toggle)"
  - "get_setting_typed(key) async accessor (thin wrapper over database.db.get_setting)"
  - "event group of handlers/admin.py SETTINGS_FIELDS/SETTINGS_GROUPS generated from the registry"
affects: [06-02, 06-03, 06-04, 06-05]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Registry module (settings_schema.py) as single source of truth for setting metadata, one-directional dependency on database.db only"
    - "Type-driven parse dispatch synthesized from parallel single-type helpers (services/reminders.py, services/scheduler.py, handlers/registration.py, handlers/admin.py)"
    - "Computed-view splice: migrated group's legacy literal tables become generated views over the registry; unmigrated groups stay literal (coexistence during incremental migration)"

key-files:
  created: [settings_schema.py]
  modified: [handlers/admin.py, tests/test_settings_groups_c0x.py]

key-decisions:
  - "D-15 (locked): _parse_setting's enum branch is `raw if raw else default` (falsy->default), NOT `raw if raw is not None else default` — matches the live `get_setting(k) or \"<default>\"` idiom byte-for-byte on empty-string, which every feature-switch consumer downstream relies on"
  - "D-10: photo/file registry entries store metadata only (label/prompt); the derived-key convention (f\"{prefix}_photo_file_id\"/f\"{prefix}_doc_file_id\") and upload-flow stay special-cased in handlers/admin.py, untouched"
  - "_parse_setting fails soft (returns raw unchanged) for any key not yet in SETTINGS_SCHEMA — this is what makes incremental group-by-group migration safe: unmigrated groups keep working exactly as before"
  - "SETTINGS_FIELDS/SETTINGS_GROUPS event portions computed via an explicit _EVENT_FIELD_ORDER list (not dict-comprehension order) to guarantee byte-identical on-screen order regardless of future registry entry insertion order"

patterns-established:
  - "Pattern: registry entry shape `{type, group, label, prompt, default}` (+ optional `options` for enum, `parse` override callable) — future waves (reg/pay/party/consent/toggle) follow this exact shape"
  - "Pattern: _parse_setting type branches lifted VERBATIM from existing pure helpers (int from _int_or_default, date from _parse_schedule_dt, list from _get_options, toggle from _is_question_on) rather than reinvented"

requirements-completed: [REG-01, REG-02, REG-03]

# Metrics
duration: 12min
completed: 2026-07-24
---

# Phase 6 Plan 1: Settings-schema Registry Bootstrap Summary

**settings_schema.py registry module (event group, 15 keys) with type-driven `_parse_setting` dispatch and `get_setting_typed` accessor; handlers/admin.py's event-group SETTINGS_FIELDS/SETTINGS_GROUPS now generated from it, byte-identical on screen — the first working vertical slice of the "one file instead of four" migration.**

## Performance

- **Duration:** 12 min
- **Started:** 2026-07-24T08:51:00Z (approx, first commit 08:51:42Z local+3)
- **Completed:** 2026-07-24T08:57:07Z
- **Tasks:** 3 completed
- **Files modified:** 3 (1 created: settings_schema.py; 2 modified: handlers/admin.py, tests/test_settings_groups_c0x.py)

## Accomplishments

- `settings_schema.py` created at repo root: `SETTINGS_SCHEMA` dict-by-key registry with the 10 event text/enum keys + 4 photo keys (program/speakers/start/venue) + 1 file key (reg_bonus), labels/prompts copied byte-for-byte from the pre-migration literal tables
- `_parse_setting(key, raw)` — pure sync type-dispatch across the full taxonomy (text/enum/photo/file/int/date/list/toggle), even though only text/enum/photo/file are populated in this plan; int/date/list/toggle branches are lifted verbatim from their source analogs so later waves can populate those groups without touching this function again
- `get_setting_typed(key)` — thin async accessor, one `get_setting` call, no duplicated raw I/O (REG-02 accessor now available to downstream consumer waves)
- `handlers/admin.py`'s event-group portion of `SETTINGS_FIELDS`/`SETTINGS_GROUPS` is now computed from `SETTINGS_SCHEMA` (REG-03 pilot) — the settings screen renders byte-identically before/after, proven by `test_event_render_snapshot`
- Four-layer regression proven for the pilot group: parse-equivalence (3 tests), coverage (1 test), render snapshot (1 test) — all green; full existing suite (359 tests) plus the 5 new tests = 364/364 green

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing regression tests** - `2d697d0` (test) — RED confirmed via ModuleNotFoundError on `settings_schema`
2. **Task 2: Create settings_schema.py registry + _parse_setting + get_setting_typed** - `87ee5a2` (feat) — GREEN, parse-equivalence + coverage tests pass
3. **Task 3: Generate event group of SETTINGS_FIELDS/SETTINGS_GROUPS from the registry** - `74bb5cc` (feat) — GREEN, full suite 364/364, no import cycle

**Plan metadata:** (this commit, following this Summary)

_Note: Task 1/2 followed the TDD RED→GREEN cycle (tdd="true"); Task 3 was a plain auto task that kept the render-snapshot test green through the wiring change._

## Files Created/Modified

- `settings_schema.py` - new module: `SETTINGS_SCHEMA` registry (event group), `_parse_setting` type-dispatch, `get_setting_typed` async accessor
- `handlers/admin.py` - imports `SETTINGS_SCHEMA`; event-group entries of `SETTINGS_FIELDS`/`SETTINGS_GROUPS` computed from the registry via a pinned `_EVENT_FIELD_ORDER`/`_EVENT_GROUP_KEYS`; all other groups' literal tuples untouched
- `tests/test_settings_groups_c0x.py` - extended (not replaced, per D-17) with 5 new tests: `test_parse_setting_text_passthrough`, `test_parse_setting_enum_falsy_to_default`, `test_parse_setting_photo_file_passthrough`, `test_registry_coverage_event`, `test_event_render_snapshot`

## Decisions Made

- **D-15 enum contract locked in code:** `_parse_setting`'s enum branch is `raw if raw else default` — verified by `test_parse_setting_enum_falsy_to_default` asserting `raw=""` resolves to default (not just `raw=None`). This is the byte-for-byte contract every live `get_setting(k) or "<default>"` feature-switch consumer depends on; using `is not None` instead would have silently changed behavior for admin-cleared (empty-string) settings.
- **Fail-soft coexistence:** `_parse_setting` returns `raw` unchanged for any key not in `SETTINGS_SCHEMA` (rather than raising `KeyError`). This is what makes it safe to run this plan's registry with only 15 of ~70 settings migrated — every unmigrated group's existing raw `get_setting(...)` call sites are completely unaffected.
- **Explicit key-order list over dict-comprehension order:** `_EVENT_FIELD_ORDER` in `handlers/admin.py` pins the on-screen order explicitly rather than relying on `SETTINGS_SCHEMA` dict insertion order — this decouples the admin.py render order from however future waves choose to interleave registry entries (e.g. if a later wave inserts a new event key in the middle of the dict, the screen order stays exactly what `_EVENT_FIELD_ORDER` says).
- **Full type-dispatch built now, not deferred:** even though only text/enum/photo/file entries exist in the registry after this plan, `_parse_setting` implements all 8 taxonomy branches (int/date/list/toggle included) so later waves (reg/pay/party/consent/toggle groups) only need to add registry entries — no changes to the dispatch function itself.

## Deviations from Plan

None - plan executed exactly as written. All three tasks, acceptance criteria, and verification commands matched the plan's `<action>`/`<acceptance_criteria>` blocks without requiring architectural changes or bug fixes.

## TDD Gate Compliance

Gate sequence confirmed in git log for this plan:
1. RED gate: `2d697d0 test(06-01): add failing registry regression tests...` — confirmed failing (ModuleNotFoundError) before commit.
2. GREEN gate: `87ee5a2 feat(06-01): create settings_schema.py registry...` — confirmed passing (parse-equivalence + coverage tests) after commit.
3. (Task 3, non-TDD `auto` task) `74bb5cc feat(06-01): generate event-group SETTINGS_FIELDS/SETTINGS_GROUPS...` — full suite confirmed green (364/364), including the render-snapshot test that spans the wiring change.

No REFACTOR commit was needed — no code smell/duplication surfaced after GREEN.

## Issues Encountered

None. `python -c "import settings_schema, handlers.admin"` confirmed no circular import (settings_schema depends only on database.db; handlers/admin.py imports settings_schema, not vice versa).

## User Setup Required

None - no external service configuration required. No `bot_settings` DB migration needed (registry reads existing rows via the unchanged `get_setting`; no schema change).

## Next Phase Readiness

- REG-01 scaffold is live: `settings_schema.py` is a working single-file registry pattern that plan 06-02+ can extend group by group (reg → pay → party → consent per D-11/D-12 ordering, then the toggle wave).
- REG-02 accessor (`get_setting_typed`) is available for any consumer wave to start reading through the registry instead of hand-rolled `get_setting(...) or "<default>"` call sites.
- REG-03 pilot proves the computed-view splice pattern (`_EVENT_FIELD_ORDER`/`_EVENT_GROUP_KEYS`) that later groups will replicate.
- No blockers. The ~590 live users' event-group settings were never touched at the data level — only the metadata source changed, confirmed by the byte-identical render snapshot.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: settings_schema.py
- FOUND: 2d697d0 (test commit)
- FOUND: 87ee5a2 (feat commit, registry)
- FOUND: 74bb5cc (feat commit, admin.py wiring)
