---
phase: 05-participant-tracks-party-delegates
plan: 02
subsystem: registration
tags: [aiogram3, sqlite-settings, tri-state-config, feature-flags]

# Dependency graph
requires:
  - phase: 05-participant-tracks-party-delegates (plan 01)
    provides: "users.participant_type / reg_started.participant_type columns, _is_party_track predicate, two-point track persistence"
provides:
  - "_is_step_enabled_for_track(setting_key, participant_type) tri-state gate resolver (D-03/D-04)"
  - "Track-aware _get_enabled_steps with D-08 overnight-only housing/bed_sharing/bed_partner skip rule"
  - "_prompt(step_key, default, participant_type=None) per-track wording resolution (D-05)"
  - "_ask_step threads participant_type to all 42 internal _prompt call sites"
  - "REG_PRESETS['party'] + _apply_party_preset() writing only __party keys (D-07)"
affects: [05-03, 05-04, 05-05, 05-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Tri-state override resolution: get_setting(key) is None -> inherit; present -> explicit on/off wins. Distinct from wording overrides, which use truthiness so an empty override string falls back instead of sending a blank message."
    - "One shared __party namespace for both party sub-tracks — sub-track differentiation is expressed as a conditional skip rule, never as a second key suffix"
    - "Bulk preset write covers every REG_FLOW entry explicitly (on or off), never only the enabled subset — guarantees idempotent re-application regardless of prior manual overrides"

key-files:
  created: []
  modified:
    - handlers/registration.py
    - tests/test_registration_phase5.py

key-decisions:
  - "D-03/D-04 (Task 1): _is_step_enabled_for_track reads {setting_key}__party only for party tracks; `is not None` (not truthiness) distinguishes explicit off from inherit — collapsing None early would make the future admin tri-state cycle (plan 05-03) impossible to build correctly."
  - "D-08 (Task 1): the new housing/bed_sharing/bed_partner skip rule is gated on _is_party_track(participant_type) AND participant_type != 'party_overnight', so it structurally cannot fire for full or None — verified by an explicit full-track regression test."
  - "D-05 (Task 2): _prompt's wording override uses truthiness, not `is not None` — an accidentally-empty reg_prompt_<step>__party falls back to global text instead of stranding the user on a blank message (T-05-02-05)."
  - "D-07 (Task 3): REG_PRESETS['party'] carries no payment_enabled key by design — plan 05-05 (D-16/D-17) owns party pricing; plan 05-03's shared preset_apply/preset_confirm handlers must tolerate a preset dict with no payment_enabled key (stated here as the cross-plan convention)."

patterns-established:
  - "Every per-track resolver (gate + wording) reads participant_type from the same source: data.get('participant_type') or 'full', resolved once per call site (_get_enabled_steps, _ask_step) — later plans (05-03..05-06) should follow this same single-resolution-point convention rather than re-deriving the track ad hoc."

requirements-completed: [TRACK-02]

# Metrics
duration: 8min
completed: 2026-07-20
---

# Phase 5 Plan 2: Party Track Question Engine — Tri-State Gates, Per-Track Wording, Seed Preset Summary

**Registration engine is now track-aware end to end: every question gate and every question prompt resolves through a single `__party` override namespace (tri-state inherit/on/off) before falling back to the global setting, overnight-only questions are skipped for `party_noovernight`, and a one-tap 🎉 Party preset seeds six questions without ever touching a full delegate's live settings.**

## Performance

- **Duration:** ~8 min (first commit 20:15:38 → last commit 20:23:22, 2026-07-20)
- **Started:** 2026-07-20T20:15:38+03:00
- **Completed:** 2026-07-20T20:23:22+03:00
- **Tasks:** 3/3 completed
- **Files modified:** 2 (handlers/registration.py, tests/test_registration_phase5.py)

## Accomplishments
- `_is_step_enabled_for_track(setting_key, participant_type)` added right after `_is_step_enabled`: for party tracks it reads `{setting_key}__party` and returns its explicit value when the key is present (`is not None` check — load-bearing, keeps "off" distinguishable from "inherit"); when absent, or for non-party tracks, it falls through to the unchanged global `_is_step_enabled`.
- `_get_enabled_steps` resolves `participant_type` once per call (`data.get("participant_type") or "full"`), gates every `REG_FLOW` step through the new tri-state resolver, and adds exactly one new skip rule: `housing`/`bed_sharing`/`bed_partner` are excluded when the track is a party track that is NOT `party_overnight` — written so it structurally cannot fire for `full`/`None`. All six pre-existing skip rules and their order are untouched.
- `_prompt` widened to `(step_key, default, participant_type=None)`: party tracks check `reg_prompt_<step_key>__party` first (truthy wins), else fall through to the existing global `reg_prompt_<step_key>` resolution unchanged. The optional third arg defaults to `None`, so the codebase's ~43 pre-existing `_prompt(...)` call sites keep compiling with byte-identical behavior.
- `_ask_step` now resolves `participant_type` once at the top (`data = await state.get_data()`) and passes it as the third positional argument to all 42 `_prompt` calls inside its body (age through `consent:*`); the one pre-flow `_prompt("full_name", ...)` call site outside `_ask_step` was intentionally left untouched (2-arg, per plan scope).
- `REG_PRESETS["party"]` added (label `"🎉 Party"`, six setting_key-spelled seed questions: `reg_q_age`, `reg_q_phone`, `reg_q_vk`, `reg_q_city`, `reg_q_allergies`, `reg_q_food`) with no `payment_enabled` key (D-07). `_apply_party_preset()` iterates every `REG_FLOW` entry and writes an explicit `on`/`off` `{setting_key}__party` key — the same "always write every step" determinism guarantee `_apply_event_preset` already provides for the forum/conf presets — and its only write is to `__party`-suffixed keys.

## Task Commits

Each task was committed atomically:

1. **Task 1: Tri-state per-track gate resolver + track-aware _get_enabled_steps (D-03, D-04, D-08)** - `027cb90` (feat)
2. **Task 2: Per-track question wording via _prompt (D-05)** - `d3123bb` (feat)
3. **Task 3: 🎉 Party seed preset writing only __party keys (D-07)** - `47746b8` (feat)

## Files Created/Modified
- `handlers/registration.py` - `_is_step_enabled_for_track`, D-08 skip rule in `_get_enabled_steps`, widened `_prompt`, track-threaded `_ask_step`, `REG_PRESETS["party"]`, `_apply_party_preset`
- `tests/test_registration_phase5.py` (new file, extended across all 3 tasks) - 26 tests: tri-state inherit/on/off + no-cross-contamination + single-namespace-for-both-subtracks + overnight skip rule + full-track regression (Task 1); wording override + truthiness fallback + 2-arg backward compat (Task 2); preset shape + REG_LABELS coverage + REG_FLOW key validity + every-step write + D-07 isolation (Task 3)

## Decisions Made
- Followed the plan's explicit action text: the D-08 skip rule uses `_is_party_track(participant_type)`, not a second call into `_is_step_enabled_for_track` — the gate resolver's only call site in this plan is the per-step loop replacement in `_get_enabled_steps`. See Deviations for the resulting acceptance-criteria discrepancy this produced.
- `_apply_party_preset` matches on `setting_key` (not `step_key`) against the preset's `"on"` set, consistent with the setting_key spelling of `REG_PRESETS["party"]["on"]` — matching on `step_key` while the list holds setting_keys would have silently written `"off"` for every question.

## Deviations from Plan

### Documented (non-code) discrepancies

**1. Task 1 acceptance criterion `grep -c "_is_step_enabled_for_track" handlers/registration.py >= 3` resolves to 2, not >= 3**
- **Found during:** Task 1 acceptance-criteria verification
- **Analysis:** The plan's own reference implementation (`05-PATTERNS.md` §"Mechanic 2") shows exactly one call site for the new resolver — the `_get_enabled_steps` loop's replacement of `_is_step_enabled(setting_key)` — matching the two occurrences actually present (the `async def` line and that one call site). The plan's Task 1 `<action>` text explicitly directs the D-08 skip rule to use `_is_party_track`, not a second call into `_is_step_enabled_for_track`, so no legitimate third call site exists within this plan's scope (`REG_FLOW`/`REG_DEFAULTS`/`REG_PRESETS`/`_prompt`/`_is_step_enabled`/`_get_enabled_steps`/`_ask_step` per the plan's `<interfaces>` block — `active_sheet_headers`, the one other place a comparable gate check exists, is explicitly D-11/D-12 sheet-routing territory reserved for plan 05-06).
- **Resolution:** No code change — fabricating an artificial extra call site purely to satisfy a grep count would have been worse than a documented, functionally-inert metric mismatch. All 9 behavioral bullets and the `<done>` criterion for Task 1 are satisfied and covered by 12 passing tests (Task 1's share of the 26-test file). Every other Task 1 acceptance check (signature shape, zero `__party_overnight`/`__party_noovernight` occurrences, REG_FLOW tuple count) passes as specified.
- **Files affected:** None (informational only).
- **Impact on plan:** None on functional correctness or test coverage; flagged here per deviation-tracking discipline rather than silently ignored.

---

**Total deviations:** 0 code changes; 1 documented acceptance-criteria discrepancy (informational, no functional impact).
**Impact on plan:** None — every `<behavior>`, `<action>`, and `<done>` requirement in all three tasks is implemented and test-covered exactly as specified.

## Issues Encountered

None. `python -m pytest tests/ -q` is fully green (175 passed) — the `apscheduler`/`sqlalchemy` environment gap noted in 05-01-SUMMARY.md is resolved (per this plan's environment notes, both are now installed in `.venv`), so the full suite collects and runs cleanly with no `--ignore` flags needed.

## Known Stubs

None. Every artifact this plan promised (`_is_step_enabled_for_track`, the D-08 skip rule, the widened `_prompt`, the track-threaded `_ask_step`, `REG_PRESETS["party"]`, `_apply_party_preset`) is fully wired and exercised by tests — no placeholder values, no hardcoded empty defaults reaching a UI surface.

## Threat Flags

None. All five `mitigate`-disposition threats in the plan's threat register (T-05-02-01..05) are addressed exactly as specified:
- T-05-02-01 (Tampering, `_apply_party_preset`): only write is `set_setting(f"{setting_key}__party", ...)` sourced from the hardcoded `REG_FLOW` list; `test_apply_party_preset_isolation_global_keys_untouched` asserts zero global-key drift.
- T-05-02-02 (Elevation of Privilege, `_is_step_enabled_for_track`): `__party` branch entered only when `_is_party_track(participant_type)` is true, and `participant_type` originates from the closed vocabulary already enforced upstream (05-01's `_extract_party_track`).
- T-05-02-03 (Information Disclosure, `_prompt`): admin-authored text returned to the caller for the single asking user; no interpolation of other users' data.
- T-05-02-04 (DoS, extra `get_setting` reads): accepted per the plan's own disposition — no caching layer added, none needed at this scale.
- T-05-02-05 (Tampering, empty prompt override): `_prompt` uses truthiness so an empty `__party` override falls back to the global text; covered by `test_prompt_empty_party_override_falls_back_to_global`.

No new network endpoints, auth paths, or trust-boundary-crossing surface was introduced beyond what the threat model already covers.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Plan 05-03 (admin UI) can now build the tri-state toggle cycle (➕ Наследует → ✅ Вкл → ❌ Выкл) directly on top of `get_setting(f"{setting_key}__party")` / `set_setting` / `delete_setting` — no new DB primitive needed, and the `is not None` semantics this plan established are exactly what the 3-state admin cycle requires.
- Plan 05-03 also needs to wire the "🎉 Party" preset button + confirm dialog calling `_apply_party_preset()` — the function and `REG_PRESETS["party"]` entry are ready; the admin-side button/dialog was explicitly out of scope for this plan.
- Plan 05-03's shared `preset_apply`/`preset_confirm` handlers must tolerate `REG_PRESETS["party"]` having no `"payment_enabled"` key (the other two presets always have one) — flagged explicitly in this SUMMARY's Decisions Made as the convention the two plans must agree on.
- Plan 05-05 (payment) is unaffected by and independent of this plan's changes — `_apply_party_preset` never writes `payment_enabled`, confirmed by test.
- Plan 05-06 (Sheets routing) will need its own track-aware header/row functions (`party_sheet_headers`/`party_sheet_row` per `05-PATTERNS.md`) — `active_sheet_headers` in `handlers/registration.py` was correctly left untouched by this plan since it serves the main (full-only) sheet.

---
*Phase: 05-participant-tracks-party-delegates*
*Completed: 2026-07-20*

## Self-Check: PASSED

All claimed files verified present (handlers/registration.py, tests/test_registration_phase5.py, this SUMMARY.md). All 3 task commits (027cb90, d3123bb, 47746b8) verified present in git log.
