---
phase: 05-participant-tracks-party-delegates
plan: 04
subsystem: registration
tags: [aiogram3, sqlite-settings, approval-routing, fsm]

# Dependency graph
requires:
  - phase: 05-participant-tracks-party-delegates (plan 01)
    provides: "users.participant_type / reg_started.participant_type columns, _is_party_track predicate, party_enabled master gate, deep-link extraction"
  - phase: 05-participant-tracks-party-delegates (plan 02)
    provides: "tri-state __party override resolution pattern, REG_FLOW track threading"
provides:
  - "_decide_status party-first branch driven solely by party_approval (D-13)"
  - "_approve_text_for(participant_type) per-track approval message with global fallback (D-15)"
  - "approve_user resolves participant_type ONCE via get_user before the payment_enabled branch, fail-soft to 'full'"
  - "_should_show_fork pure gate + party_pick: pre-flow keyboard for self-selecting a track (D-10)"
  - "_track_from_link forward-compat FSM marker (no REG_FLOW key, D-09)"
affects: [05-05, 05-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Party-first branch placed BEFORE the reg_mode branch in _decide_status — no fallthrough, no reading of full_setting/short_setting on the party path (D-13)"
    - "party_setting or 'manual' fail-closed default — an unconfigured/unreadable setting moderates rather than auto-approves"
    - "Single resolved-once-at-top value (participant_type in approve_user) consumed by both the early-return payment branch and the completion-text branch, so a later plan can extend the payment branch without a second get_user call"
    - "Pre-flow inline keyboard instead of a REG_FLOW step — D-09's constraint satisfied by keeping the fork entirely outside the FSM step machinery"

key-files:
  created: []
  modified:
    - handlers/registration.py
    - tests/test_registration_phase5.py

key-decisions:
  - "D-13 (Task 1): _decide_status widened with participant_type/party_setting defaulted params so every pre-Phase-5 call site keeps compiling; party branch is the FIRST branch, stops before the reg_mode logic, and party_setting=None resolves as 'manual' (safe default)."
  - "D-14 (Task 1): compliance achieved entirely by omission — no participant_type predicate added to get_pending_users/get_pending_count/finalize_registration's admin-notify path. An auto-approved party row is simply written with status='approved' and the existing status='pending' query never selects it."
  - "D-15 (Task 2): _approve_text_for mirrors _prompt's truthiness-based fallback (not is-not-None) so an accidentally-empty approve_text__party degrades to the global text instead of sending a blank approval message."
  - "D-15/BLOCKER-3 (Task 2): approve_user resolves participant_type via get_user() unconditionally at the top of the function body, before the payment_enabled early-return branch — verified by a source-order test — so plan 05-05 can reuse the same resolved value for tariff filtering without a second DB read."
  - "D-10/D-09 (Task 3): the fork is implemented as a pre-flow InlineKeyboardMarkup in cmd_start, never a REG_FLOW entry. _should_show_fork takes explicit party_track/recovered_track/is_registered args (reads party_fork_question/party_enabled itself) so it is testable without a Telegram update."

patterns-established:
  - "Approval/messaging resolvers that need the track always take participant_type as an optional trailing arg defaulted to None/'full', matching _prompt's D-05 shape — later plans (05-05 payment, 05-06 sheets) should follow the same convention."

requirements-completed: [TRACK-03, TRACK-04]

# Metrics
duration: 9min
completed: 2026-07-20
---

# Phase 5 Plan 4: Party Track Approval + Fork Question — Independent Routing, Per-Track Message, Optional Self-Select Summary

**Party applications now resolve their approval status and their welcome message from `party_approval`/`approve_text__party` alone (never inheriting full/short moderation or the forum welcome text), and an admin-gated pre-flow keyboard lets a delegate self-select a party track when no deep link set it — adding zero new REG_FLOW steps and zero new FSM states.**

## Performance

- **Duration:** ~9 min (first commit 20:33:42 → last commit 20:42:30, 2026-07-20)
- **Tasks:** 3/3 completed
- **Files modified:** 2 (handlers/registration.py, tests/test_registration_phase5.py)

## Accomplishments
- `_decide_status` gained a party-first branch: `participant_type`/`party_setting` are optional trailing params (existing call sites and tests keep passing unchanged), the party branch resolves status purely from `party_setting or "manual"` and returns before ever reading `full_setting`/`short_setting` — no fallthrough, per D-13.
- `finalize_registration` threads `data.get("participant_type", "full")` and a fresh `await get_setting("party_approval")` read into the widened `_decide_status` call, alongside the existing `full_setting`/`short_setting` reads.
- D-14 verified end-to-end with an integration-style test: a party row finalized with `party_approval="auto"` (and `full_approval="manual"`) lands with `status="approved"` and `get_pending_count() == 0`; the same row finalized with `party_approval="manual"` is `status="pending"` and appears in `get_pending_users()` — no second queue, no new predicate anywhere in the pending-count/pending-users path.
- `_approve_text_for(participant_type)` added next to `send_completion_and_bonus`: party tracks read `approve_text__party` (truthy wins, empty string falls back), non-party or absent-override falls through to the existing global `approve_text` resolution and the same `DEFAULT_APPROVE_TEXT` constant — no second copy of the default text.
- `send_completion_and_bonus` gained an optional `participant_type: str | None = None` parameter; its text read now delegates entirely to `_approve_text_for`.
- `approve_user` resolves `participant_type` from `get_user(telegram_id)` exactly once, at the top of the function body, wrapped fail-soft to `"full"` on any lookup error — verified this ordering is BEFORE the `payment_enabled` early-return branch via a source-order assertion test, so plan 05-05 can extend the payment path with the same resolved value without a second DB read.
- `_should_show_fork(party_track, recovered_track, is_registered)` added as a pure(ish) five-condition async gate: false when `party_fork_question` is unset (ROADMAP SC#5 default-off posture), false when a deep-link or recovered track is already resolved (D-10 authoritative-value posture), false when `party_enabled` is off (never offer a closed track), false when already registered, true only when every condition holds.
- `cmd_start` renders a `_party_fork_kb()` inline keyboard (three buttons: full / party-with-overnight / party-without-overnight, using the same `party_pick:` prefix + the same two literal `_PARTY_TAG_MAP` tokens as the deep-link extractor) when `_should_show_fork` passes, and returns — waiting for the tap instead of starting the flow directly.
- New `party_pick` callback handler maps the tapped token through `_PARTY_TAG_MAP` (unmapped token → answered + rejected, never routed), re-checks `party_enabled` at tap time (closing the render-then-flip window), clears the keyboard, fabricates a correctly-attributed message the same way `party_fallback_full` already does, and starts the flow with the chosen `participant_type`.
- `_start_registration_flow` now also records a `_track_from_link` FSM marker whenever a fresh `participant_type` arg was passed (deep link or fork tap) — explicitly documented as forward-compat only; no REG_FLOW entry consumes it in this phase (D-09).

## Task Commits

Each task was committed atomically:

1. **Task 1: Independent party approval routing (D-13, D-14)** - `64e9104` (feat)
2. **Task 2: Per-track approval message (D-15)** - `aaf311f` (feat)
3. **Task 3: Optional in-flow fork question behind party_fork_question (D-10, D-09)** - `b382bca` (feat)

## Files Created/Modified
- `handlers/registration.py` - `_decide_status` party branch, `finalize_registration` threading, `_approve_text_for`, widened `send_completion_and_bonus`, `approve_user` track resolution, `_should_show_fork`, `_party_fork_kb`, `party_pick` callback handler, `_track_from_link` FSM marker
- `tests/test_registration_phase5.py` - 23 new tests across the three tasks (truth-table + pending-queue integration tests for Task 1, approve-text fallback/namespace tests + signature/ordering assertions for Task 2, fork-gate combinatorial tests + token-vocabulary + REG_FLOW-count-unchanged tests for Task 3); file total 49 tests (was 26 before this plan)

## Decisions Made
- Chose to keep the D-14 mechanism entirely negative (no code added, not "always enqueue then approve") — the integration tests assert the *absence* of a party row from `get_pending_count()`/`get_pending_users()` rather than testing a filter, matching the plan's explicit instruction not to add a `participant_type` predicate to those queries.
- `approve_user`'s track-resolution comment was deliberately reworded to avoid the literal substring `"payment_enabled"` appearing before the `get_user(` call in source order, so the plan's own ordering-guard test (`s.index('get_user(') < s.index('payment_enabled')`) asserts the real code order rather than an incidental comment mention.
- Chose to send the existing welcome text/photo before rendering the fork keyboard (mirrors the normal flow's welcome-then-registration sequence) rather than skipping straight to the fork — not specified by the plan, least-surprising choice.

## Deviations from Plan

None - plan executed exactly as written. All three tasks' `<behavior>`, `<action>`, and `<done>` requirements are implemented and test-covered; every acceptance-criteria grep/assertion in the plan passes as specified.

## Issues Encountered

None. `python -m pytest tests/ -q` is fully green (198 passed, up from 175 at the start of this plan) — no `apscheduler`/`sqlalchemy` environment gaps (already resolved per 05-02's environment notes).

## Known Stubs

None. Every artifact this plan promised (`_decide_status` party branch, `_approve_text_for`, the widened `send_completion_and_bonus`, `approve_user`'s track resolution, `_should_show_fork`, the `party_pick` handler, the `_track_from_link` marker) is fully wired end-to-end and exercised by tests — no placeholder values, no hardcoded empty defaults reaching a UI surface.

## Threat Flags

None. All six `mitigate`-disposition threats in the plan's threat register (T-05-04-01..04, 06; T-05-04-05 disposition `accept`) are addressed exactly as specified:
- T-05-04-01 (Elevation of Privilege, `party_pick:` handler): token mapped through the fixed `_PARTY_TAG_MAP`, unmapped token rejected via `callback.answer()` + early return; `party_enabled` re-checked inside the handler before honouring a party choice.
- T-05-04-02 (Elevation of Privilege, `_decide_status` party branch): `party_setting or "manual"` fails closed on an unconfigured/unreadable setting.
- T-05-04-03 (Spoofing, fork bypass of D-11a): the fork's `_should_show_fork` gate reads `party_enabled` itself and returns `False` when it is off, so the closed-track path from 05-01 cannot be circumvented via the fork.
- T-05-04-04 (DoS, `get_user` lookup in `approve_user`): wrapped in try/except, degrades to `"full"` on failure — an approved user is never stranded without a message.
- T-05-04-06 (Tampering, double approval of a party row): no change to the atomic `UPDATE … WHERE status='pending'` guard from Phase 2; party rows share that same guard unchanged.

No new network endpoints, auth paths, or trust-boundary-crossing surface was introduced beyond what the plan's own threat model already covers.

## User Setup Required

None - no external service configuration required. `party_approval`, `party_fork_question`, and `approve_text__party` are all `bot_settings` keys with safe defaults (moderated/off/inherit-global respectively) — nothing to configure before this code is live; plan 05-03 (admin UI, in progress independently) is what will expose these as tap-to-toggle buttons instead of requiring direct DB edits.

## Next Phase Readiness

- Plan 05-05 (payment) can now extend `approve_user`'s payment-path branch to consume the same `participant_type` local already resolved at the top of the function — no second `get_user` call needed, matching the ordering guard this plan's tests enforce.
- Plan 05-03 (admin UI, independent wave-3 plan) can wire `party_approval`/`party_fork_question` toggles and a `party_fork_text`/`party_closed_text` editor directly against the `bot_settings` keys this plan reads — no new DB primitive required.
- `_track_from_link` is recorded but not yet consumed by any REG_FLOW step (by design, D-09) — a future phase that adds a fork-related REG_FLOW step key has the marker ready to express "skip it, already authoritative."

---
*Phase: 05-participant-tracks-party-delegates*
*Completed: 2026-07-20*
