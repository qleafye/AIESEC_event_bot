---
phase: quick-260724-cfn
plan: 01
subsystem: ui
tags: [aiogram, admin-ui, settings, party-track, telegram-callbacks]

# Dependency graph
requires:
  - phase: 05-participant-tracks-party-delegates
    provides: "party track runtime (participant_type, __party override truthiness fallback D-05/D-15, payment_options track filter D-16)"
provides:
  - "approve_text__party editable via existing settings_edit machinery (under 🎉 Party sub-screen)"
  - "Full/Party track switcher on the «✏️ Тексты вопросов» screen (reg_prompt_track: callbacks)"
  - "payment_options help text documents the exact track-filter syntax (full/party_overnight/party_noovernight)"
affects: [05-participant-tracks-party-delegates, admin-settings-ux]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Track-switcher screen pattern reused a second time (reg_q_track: for question toggles, reg_prompt_track: for prompt-text editors) — same shape (_track_switcher_row-style row, re-render same message via edit_text), each in its own callback_data namespace"
    - "Optional trailing callback_data suffix (':party') parsed via split(':') with a closed whitelist fallback to 'full', rather than a new FSM state or new handler per track"

key-files:
  created: []
  modified:
    - handlers/admin.py
    - tests/test_admin_phase5.py

key-decisions:
  - "approve_text__party reuses settings_edit_start/settings_edit_value unchanged — added only to SETTINGS_FIELDS, HTML_SETTINGS, and SETTINGS_GROUPS['party']; zero new callbacks/FSM states for WR-02a"
  - "reg_prompt_track_switch and the track-suffixed reg_prompt_edit are new but mirror the existing reg_q_track_switch/build_questions_keyboard pattern exactly, including the admin-gate-first line and the closed full/party whitelist"
  - "reg_prompt_edit callback_data suffix parse uses split(':') positionally (parts[2] == 'party') rather than rsplit, since step_keys never contain ':' — kept identical to the plan's specified parsing"

patterns-established: []

requirements-completed: [WR-02a, WR-02b, WR-05]

# Metrics
duration: 25min
completed: 2026-07-24
---

# Quick Task 260724-cfn: Per-Track Party Admin UI Summary

**Closed three Phase-5 code-review deferrals (WR-02a/WR-02b/WR-05) by exposing per-track party approve-text, prompt-text overrides, and the payment_options track-filter syntax through the existing bot admin UI — no runtime override semantics changed.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-07-24T09:12:00+03:00 (approx, plan/context read)
- **Completed:** 2026-07-24T09:38:48+03:00
- **Tasks:** 2 completed
- **Files modified:** 2 (handlers/admin.py, tests/test_admin_phase5.py)

## Accomplishments
- `approve_text__party` is now editable from the bot (🎉 Party sub-screen), saves HTML, and "-" resets to inheriting the global `approve_text` — closing WR-02a (previously only settable via direct `bot_settings` SQL write).
- The «✏️ Тексты вопросов» screen now has a Полный ⇄ Party track switcher; in Party mode, buttons edit `reg_prompt_{step}__party` instead of the global `reg_prompt_{step}` — closing WR-02b. Global editor behavior for the unsuffixed callback is byte-identical to before.
- `payment_options` help text now documents the exact optional third field (track filter) syntax matching `handlers/payment.py::_parse_options` precisely — closing WR-05.

## Task Commits

Each task was committed atomically:

1. **Task 1: WR-02a approve_text__party editor + WR-05 payment_options help** - `f61518d` (feat)
2. **Task 2: WR-02b track switcher on «Тексты вопросов»** - `2b29037` (feat)

**Plan metadata:** (SUMMARY/STATE commit handled by orchestrator, not this agent)

## Files Created/Modified
- `handlers/admin.py` - Added `approve_text__party` to SETTINGS_FIELDS/HTML_SETTINGS/SETTINGS_GROUPS['party']; extended payment_options help text with the track-filter syntax; added `_prompt_track_switcher_row`, `render_prompts_text`, `build_prompts_keyboard`, `reg_prompt_track_switch`; `admin_reg_prompts` now renders via the new helpers (track="full"); `reg_prompt_edit` parses an optional `:party` callback suffix to target `reg_prompt_{step}__party`.
- `tests/test_admin_phase5.py` - Added 8 new tests: 2 for Task 1 (field/group/HTML registration, help text content), 6 for Task 2 (keyboard callback shape per track, track-switch handler re-render + non-admin rejection, edit-handler FSM setting_key for both suffixed/unsuffixed callbacks, default-render regression). Also added a real-`FSMContext`+`MemoryStorage` test helper (`_new_state`, mirroring `tests/test_registration_phase5.py`) since this plan's tests needed to assert on FSM `setting_key` data, which the existing `FakeCallback`/`FakeMessage` stand-ins don't carry.

## Decisions Made
- Kept WR-02a to a pure registry addition (no new handler) — `settings_edit_start`/`settings_edit_value` already handle any key present in `SETTINGS_FIELDS`, matching the plan's explicit "do not create new callbacks/FSM" instruction.
- Mirrored the existing `reg_q_track:`/`build_questions_keyboard` pattern verbatim for the new `reg_prompt_track:` switcher rather than generalizing/sharing code between the two screens — plan explicitly asked to mirror, not refactor, to keep the diff minimal and risk low.
- Did not touch `_approve_text_for` (registration.py) or `_parse_options` (payment.py) runtime logic anywhere — both already correctly implement the D-05/D-15/D-16 fallback semantics per the 05-REVIEW.md findings; this plan is UI/help-text only.

## Deviations from Plan

None - plan executed exactly as written. Both tasks matched their `<action>` specs; helper names, callback_data shapes, and whitelist logic match the plan's interface contract 1:1.

## Issues Encountered
- The worktree checkout has no `.env` file (gitignored, not present in worktrees), so `pydantic-settings` failed to load `config.Settings()` during test collection. Ran tests with `BOT_TOKEN`/`ADMIN_IDS` env vars set inline for the test invocation only (no files changed, not committed) — this is a pre-existing test-environment gap unrelated to this plan's scope, not a code deviation.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All three Phase-5 WR-02a/WR-02b/WR-05 code-review deferrals are closed; 05-REVIEW.md can be marked resolved for these items.
- Existing global `approve_text` and `reg_prompt_{step}` editors verified unaffected (regression tests + full 345-test suite pass).
- No blockers identified for Phase-5 end-of-phase UAT.

---
*Phase: quick-260724-cfn*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: handlers/admin.py
- FOUND: tests/test_admin_phase5.py
- FOUND: .planning/quick/260724-cfn-wr-02-wr-05-admin-ui-per-track-party-app/260724-cfn-SUMMARY.md
- FOUND: commit f61518d
- FOUND: commit 2b29037
