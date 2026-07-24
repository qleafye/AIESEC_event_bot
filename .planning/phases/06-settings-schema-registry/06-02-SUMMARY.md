---
phase: 06-settings-schema-registry
plan: 02
subsystem: config
tags: [settings-registry, admin-ui, aiogram3, type-dispatch]

# Dependency graph
requires: ["06-01"]
provides:
  - "settings_schema.SETTINGS_SCHEMA extended with reg/pay/party/consent groups (22 keys: 10 reg, 7 pay, 3 party, 2 consent)"
  - "handlers/admin.py SETTINGS_FIELDS/SETTINGS_GROUPS fully generated from the registry (event+reg+pay+party+consent — no hand-written literal tuples remain for any text-surface group)"
  - "_SETTINGS_DISPLAY_DEFAULTS derived from registry `default` values (type==\"text\" filter) instead of a separate literal dict"
affects: [06-03, 06-04, 06-05, 06-06, 06-07]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-group pinned _<GROUP>_FIELD_ORDER lists (mirrors 06-01's _EVENT_FIELD_ORDER) — extended to all four remaining text-surface groups so SETTINGS_GROUPS/SETTINGS_FIELDS on-screen order stays byte-identical regardless of registry dict insertion order"
    - "_SETTINGS_DISPLAY_DEFAULTS derivation scoped to type==\"text\" with a non-empty default — prevents a functional parse-fallback default (int/date) from leaking into the UI's \"по умолчанию\" display flag"

key-files:
  created: []
  modified: [settings_schema.py, handlers/admin.py, tests/test_settings_groups_c0x.py]

key-decisions:
  - "T-06-07 (locked): pending_reminder_interval's registry default is pinned to 1800 (int), verified byte-for-byte against services.reminders._reminder_interval across a None/empty/garbage/negative/positive input matrix — reminder cadence cannot silently drift from the registry migration"
  - "T-06-06 (locked): party_closed_text/party_sheet_tab move their display-fallback strings from the old literal _SETTINGS_DISPLAY_DEFAULTS dict into the registry entry's `default` field; the dict is now DERIVED (`{k: v[\"default\"] for k,v in SETTINGS_SCHEMA.items() if v[\"type\"]==\"text\" and v.get(\"default\") not in (None, \"\")}`), restricted to `type==\"text\"` specifically so pending_reminder_interval's int default (1800) is never picked up as a display default"
  - "D-15 list-type contract confirmed: the registry's list branch (lifted in 06-01) extends splitlines/strip with a `;` inline separator (Telegram Enter=send trap convention) — proven equivalent via test_parse_equivalence_list; this is a superset of the current keyboards/builders.py `_get_options`-style `\\n`-only split, intentional per the project's `;`-separator convention, not a byte-for-byte match with every legacy split call site"
  - "No per-type parse functions added — Task 2 reused 06-01's existing _parse_setting dispatch entirely unchanged; only registry data (labels/prompts/types/defaults) was added"

patterns-established:
  - "Registry entry shape stayed exactly `{type, group, label, prompt, default}` for all 22 new keys — no shape drift from the 06-01 pilot"
  - "Migrating a group is now a two-part recipe: (1) add SETTINGS_SCHEMA entries with byte-for-byte copied label/prompt, correct type, and default; (2) add one pinned `_<GROUP>_FIELD_ORDER` list + one list-comprehension line in admin.py, then splice it into SETTINGS_FIELDS/SETTINGS_GROUPS — the toggle wave (06-04) can follow this exact recipe once its own admin.py UI-generation mechanics are decided"

requirements-completed: []  # REG-01/REG-03 remain phase-wide (span 06-01..06-07); toggle-group keys are not yet in the registry (D-12 defers that wave to 06-04) and REG-02 consumers have not started (06-03) — marking REG-01/03 "Complete" here would be premature, same rationale as 06-01's SUMMARY. Will mark once 06-07's final coverage plan closes the phase.

# Metrics
duration: 18min
completed: 2026-07-24
---

# Phase 6 Plan 2: Reg/Pay/Party/Consent Settings Groups Summary

**Migrated all remaining text-surface admin settings groups (reg: 10 keys, pay: 7 keys, party: 3 keys, consent: 2 keys — 22 total) into `SETTINGS_SCHEMA` with correct type taxonomy (int/date/list/text), byte-for-byte preserving admin UI render order/labels/flags while proving int/date/list parse-equivalence against the live production oracles.**

## Performance

- **Duration:** 18 min
- **Completed:** 2026-07-24
- **Tasks:** 2 completed
- **Files modified:** 3 (settings_schema.py, handlers/admin.py, tests/test_settings_groups_c0x.py)

## Accomplishments

- `settings_schema.SETTINGS_SCHEMA` extended with 22 new entries spanning 4 groups (reg/pay/party/consent), each with byte-for-byte copied `label`/`prompt` strings from the pre-migration literal `SETTINGS_FIELDS` tuples in `handlers/admin.py`
- Correct per-key type assignment: `pending_reminder_interval` → `int` (default pinned to `1800`, matching `services/reminders.py::DEFAULT_INTERVAL`); `payment_deadline` → `date`; `source_options`/`city_options`/`study_field_options`/`goal_options`/`formats_options`/`university_options`/`payment_options`/`payment_requisites_by_lc`/`penalty_schedule`/`consent_list` → `list`; everything else (`reg_complete_text`, `approve_text`, `reject_text`, `payment_requisites`, `payment_reminder_text`, `payment_overdue_text`, `party_closed_text`, `party_sheet_tab`, `approve_text__party`, `consent_button_text`) → `text`
- `handlers/admin.py`'s `SETTINGS_FIELDS`/`SETTINGS_GROUPS` for these 4 groups now generated from the registry via pinned `_REG_FIELD_ORDER`/`_PAY_FIELD_ORDER`/`_PARTY_FIELD_ORDER`/`_CONSENT_FIELD_ORDER` lists — same computed-view splice pattern as the 06-01 event pilot. Combined with 06-01's event-group migration, **every entry in `SETTINGS_FIELDS`/`SETTINGS_GROUPS` is now registry-generated** — no hand-written literal tuples remain for any text/list/int/date/photo/file key (only the toggle-button block in `build_settings_keyboard` stays hardcoded, deferred per D-12 to a later wave)
- `_SETTINGS_DISPLAY_DEFAULTS` (the "по умолчанию" flag source) is now derived from the registry's `default` field instead of a separate literal dict, scoped to `type == "text"` entries with a non-empty default — this is what keeps `party_closed_text`/`party_sheet_tab` showing "по умолчанию" while `pending_reminder_interval`'s functional int default (`1800`) does NOT leak into the display-flag logic
- Parse-equivalence proven byte-for-byte for all three non-text types migrated in this plan: `test_parse_equivalence_int` against `services.reminders._reminder_interval` (None/""/"abc"/"0"/"-5"/"900"/"1800"), `test_parse_equivalence_date` against `services.scheduler._parse_schedule_dt` (None/""/"garbage"/valid datetime), `test_parse_equivalence_list` against the splitlines+`;`-split reference implementation (None/""/multi-line/`;`-separated)
- Four new render-snapshot tests (`test_render_snapshot_reg/pay/party/consent`) lock in label text, on-screen order, and `settings_edit:`/noop/back callback-data for each group — proven byte-identical before and after the registry-driven generation swap
- Full regression: `tests/test_settings_groups_c0x.py` 22/22 green; `tests/test_registration_phase4.py` + `tests/test_registration_phase5.py` 77/77 green; full project suite 372/372 green

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing parse-equivalence + render-snapshot tests** - `e15f93e` (test) — RED confirmed: `test_parse_equivalence_int/date/list` and `test_registry_coverage_all_text_groups` failed (keys absent from registry); render-snapshot tests passed against the still-literal pre-migration admin.py, establishing the byte-identical baseline
2. **Task 2: Add reg/pay/party/consent entries + generate admin views** - `05da585` (feat) — GREEN, all 22 tests in the file pass, full suite 372/372

_Note: Task 1 followed the TDD RED→GREEN cycle (tdd="true"); Task 2 was a plain auto task that kept every render-snapshot test green through the wiring change (D-16 byte-for-byte proof)._

## Files Created/Modified

- `settings_schema.py` - added 22 registry entries across reg/pay/party/consent groups (types: int×1, date×1, list×10, text×10)
- `handlers/admin.py` - `_REG_FIELD_ORDER`/`_PAY_FIELD_ORDER`/`_PARTY_FIELD_ORDER`/`_CONSENT_FIELD_ORDER` pinned key-order lists; `SETTINGS_FIELDS`/`SETTINGS_GROUPS` now fully computed from `SETTINGS_SCHEMA` for these groups; `_SETTINGS_DISPLAY_DEFAULTS` re-derived from the registry
- `tests/test_settings_groups_c0x.py` - extended (not replaced) with 8 new tests: `test_parse_equivalence_int`, `test_parse_equivalence_date`, `test_parse_equivalence_list`, `test_registry_coverage_all_text_groups`, `test_render_snapshot_reg`, `test_render_snapshot_pay`, `test_render_snapshot_party`, `test_render_snapshot_consent`

## Decisions Made

- **T-06-07 pinned int default:** Registry's `pending_reminder_interval` entry hardcodes `"default": 1800`, matching `services/reminders.py::DEFAULT_INTERVAL` exactly — verified byte-for-byte via `test_parse_equivalence_int`, closing the threat of a silent reminder-cadence drift for live admins.
- **T-06-06 scoped display-default derivation:** `_SETTINGS_DISPLAY_DEFAULTS` in `handlers/admin.py` is now `{k: v["default"] for k, v in SETTINGS_SCHEMA.items() if v["type"] == "text" and v.get("default") not in (None, "")}` — the `type == "text"` filter is load-bearing: without it, `pending_reminder_interval`'s int default of `1800` would also satisfy "non-empty default" and incorrectly flag that field as "по умолчанию" in the reg-group sub-screen (which it never did pre-migration). Caught by writing `test_render_snapshot_reg` to assert the plain "не задано" flag for that field.
- **List-type contract preserved from 06-01, not re-litigated:** The list branch of `_parse_setting` (already lifted in 06-01 with the `;`-separator extension) was reused unchanged for all 10 new list-type keys (source_options, city/study_field/goal/formats/university_options, payment_options, payment_requisites_by_lc, penalty_schedule, consent_list). No new parse code was written per the plan's explicit instruction to reuse the existing dispatch.
- **No architectural changes needed:** Task 2 was a pure additive registry-data + computed-view change; no new tables, no schema changes, no new dependencies.

## Deviations from Plan

None — plan executed exactly as written. Task 1 produced the expected RED (parse-equivalence + coverage tests failed on missing keys; render-snapshot tests passed as the pre-migration baseline). Task 2 produced the expected GREEN with zero drift in any of the four groups' render output.

## TDD Gate Compliance

Gate sequence confirmed in git log for this plan:
1. RED gate: `e15f93e test(06-02): add failing parse-equivalence + render-snapshot tests...` — confirmed failing (4 of 22 tests: parse-equivalence int/date/list + coverage) before the commit.
2. GREEN gate: `05da585 feat(06-02): migrate reg/pay/party/consent settings groups...` — confirmed passing (all 22 tests in the file, plus full 372-test suite) after the commit.

No REFACTOR commit was needed — no code smell/duplication surfaced after GREEN; the per-group `_<GROUP>_FIELD_ORDER` pattern was a direct, unmodified extension of 06-01's `_EVENT_FIELD_ORDER` precedent.

## Issues Encountered

None. Confirmed no circular import (`settings_schema.py` still imports only `database.db`; `handlers/admin.py` imports `settings_schema`, not vice versa) — same invariant as 06-01, unchanged by this plan.

## User Setup Required

None — no external service configuration required. No `bot_settings` DB migration needed; the registry reads existing rows via the unchanged `get_setting`, and no schema change occurred. All ~590 live users' reg/pay/party/consent settings were never touched at the data level — only the metadata source changed.

## Next Phase Readiness

- REG-01 scaffold now covers ALL text/list/int/date/photo/file keys across every admin settings group (event, reg, pay, party, consent) — only the toggle-button wave (D-12, planned for 06-04) remains outside the registry.
- REG-03 pilot pattern (computed-view splice, `_<GROUP>_FIELD_ORDER` lists) has now been replicated 4 additional times with zero deviation — 06-04's toggle-button generation can follow the same recipe once its own UI-mechanics decision (D-12's discretionary note) is made.
- 06-03 (REG-02 consumers: `services/reminders.py`, `services/scheduler.py`, `keyboards/builders.py`) can now safely read `pending_reminder_interval`/`payment_deadline`/`source_options` etc. through `get_setting_typed` — the parse-equivalence tests in this plan prove the registry's parse output is byte-for-byte identical to what those consumers' own hand-rolled parse helpers already produce, so switching call sites carries zero behavior-change risk.
- No blockers.

---
*Phase: 06-settings-schema-registry*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: settings_schema.py (source contains "pending_reminder_interval" with "type": "int" and "payment_deadline" with "type": "date")
- FOUND: e15f93e (test commit)
- FOUND: 05da585 (feat commit)
