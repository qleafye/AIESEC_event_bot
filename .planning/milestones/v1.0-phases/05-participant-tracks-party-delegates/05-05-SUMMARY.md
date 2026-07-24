---
phase: 05-participant-tracks-party-delegates
plan: 05
subsystem: payments
tags: [aiogram3, sqlite-settings, tariff-filtering, inline-keyboard]

# Dependency graph
requires:
  - phase: 05-participant-tracks-party-delegates (plan 01)
    provides: "users.participant_type column, _is_party_track predicate"
  - phase: 05-participant-tracks-party-delegates (plan 04)
    provides: "approve_user resolves participant_type ONCE via get_user before the payment_enabled branch, fail-soft to 'full'"
provides:
  - "_parse_options 3-tuple (label, price, tracks) with optional third pipe-delimited track field, 2-field lines byte-identical to Phase 4 (D-16)"
  - "_visible_options(options, participant_type) pure index-preserving filter helper (D-17)"
  - "start_payment_step(bot, telegram_id, participant_type='full') renders only track-eligible tariffs, single/free fallback reads the visible list"
  - "D-18 free-completion routing when no tariff matches a track"
  - "process_payment_option server-side track-eligibility re-check (T-05-05-03)"
affects: [05-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Positional callback_data index preserved under filtering: build a (index, label, price) visible list by enumerate()-ing the FULL unfiltered source once, never re-enumerate the filtered result"
    - "Single/free fallback always reads from the filtered/visible list's first entry, never the unfiltered list's index 0 — same shape a later per-track feature should follow whenever a fallback exists alongside a filter"
    - "Server-side re-validation of a client-tapped index against freshly-resolved state (not the state implied by the keyboard that was rendered) — closes a TOCTOU-style gap between keyboard render time and tap time"

key-files:
  created:
    - tests/test_payment_phase5.py
  modified:
    - handlers/payment.py
    - handlers/registration.py

key-decisions:
  - "D-16 (Task 1): _parse_options widened to 3-tuples via unlimited '|' split (not split(\"|\", 1)) so a third field is recoverable; blank third field ('label|price|') resolves to None (all tracks), never an empty set, which would silently mean 'matches nobody'"
  - "D-17 (Task 2): _visible_options is a standalone pure function (not inlined into start_payment_step) specifically so the index-preservation property is directly unit-testable without a Telegram send path — the D-17 safeguard is the function boundary itself, not just its output"
  - "T-05-05-07 (Task 2): the single/free fallback in start_payment_step was rewritten to read visible[0], not options[0] — the plan's own review caught that the pre-existing 'compute paid from the full list, fall back to options[0]' shape becomes a silent wrong-price bug the moment tariffs are track-split"
  - "T-05-05-03 (Task 2): process_payment_option's eligibility re-check is skipped entirely (no get_user call) when the tapped option's tracks is None — matches every existing 2-field payment_options line's behavior unchanged, and avoids a DB read on the common no-filtering path"

patterns-established:
  - "Filter-then-render-not-filter-then-reindex: any future feature adding a filtered view onto a positionally-indexed callback_data list must build (original_index, ...) tuples from a single enumerate() over the unfiltered source, exactly like _visible_options"

requirements-completed: [TRACK-05]

# Metrics
duration: 12min
completed: 2026-07-21
---

# Phase 5 Plan 5: Party Track Payment Filtering — Track-Split Tariffs Without Breaking the pay_option:{i} Index Contract Summary

**`payment_options` now accepts an optional `|track` suffix so party guests see only their tariff (never the three-day forum price), while `pay_option:{i}` keeps indexing the full unfiltered list — a keyboard rendered before a settings edit can never resolve to a shifted tariff, and a stale/foreign-track tap is now rejected server-side.**

## Performance

- **Duration:** ~12 min (first commit 02:48:14 → last commit 02:50:54, 2026-07-21)
- **Tasks:** 2/2 completed
- **Files modified:** 2 (handlers/payment.py, handlers/registration.py)
- **Files created:** 1 test file

## Accomplishments
- `_parse_options` returns `list[tuple[str, int, set[str] | None]]`: a bare `label|price` line parses byte-identical to before Phase 5 (`tracks=None`), and an optional third field (`label|price|party_overnight,party_noovernight`) yields a stripped, comma-split track set. Blank third field yields `None`, not an empty set — verified this doesn't collapse to "matches nobody."
- All four 2-element unpack sites widened to 3 elements: `start_payment_step` (keyboard build), `process_payment_option`, and `_payment_price_block` in `handlers/registration.py` (price preview, track element unused there by design — filtering is post-approval only).
- `_visible_options(options, participant_type)` added as a standalone pure helper: enumerates the FULL options list once and emits `(i, label, price)` for tracks-eligible entries, with `i` always the original index — proven by a dedicated test that a party-only tariff at index 1 keeps index 1 for a party caller and is simply absent (not renumbered) for a full caller.
- `start_payment_step` gained `participant_type: str = "full"`; the multi-option keyboard, the paid-count check, and the single/free fallback all now read from `visible`, never the unfiltered `options` list — closing a wrong-price bug (T-05-05-07) where a party-only tariff sitting at a non-zero index would have been shadowed by a different track's tariff at index 0.
- D-18 implemented: when `_visible_options` returns empty (no tariff matches the caller's track), `start_payment_step` routes straight to `send_completion_and_bonus` — the exact same outcome as `payment_enabled=off` — instead of sending an empty/dead keyboard.
- `process_payment_option` gained a server-side eligibility re-check (T-05-05-03): for any tapped option carrying a non-`None` track set, it resolves the caller's CURRENT track via `get_user` (never trusts the tap itself) and rejects with an alert — before `update_payment_status` or `_show_payment_details` run — if the current track isn't in the option's set. Options with `tracks=None` skip the check entirely (no extra DB read on the common unfiltered path). The pre-existing bounds check on the tapped index was preserved unchanged.
- `approve_user` threads the `participant_type` it already resolves once (05-04's ordering guarantee) into `start_payment_step` — no second `get_user` call added.

## Task Commits

Each task was committed atomically:

1. **Task 1: Optional third `track` field in payment_options (D-16)** - `cabc940` (feat)
2. **Task 2: Track-filtered keyboard with preserved indices + free fallback (D-17, D-18)** - `84ff379` (feat)

## Files Created/Modified
- `handlers/payment.py` - `_parse_options` 3-tuple widening, new `_visible_options` helper, `start_payment_step(..., participant_type)` track-filtered rendering + D-18 free routing, `process_payment_option` T-05-05-03 eligibility re-check
- `handlers/registration.py` - `_payment_price_block` unpack widened (informational preview only, no filtering), `approve_user` threads `participant_type` into `start_payment_step`
- `tests/test_payment_phase5.py` (new) - 22 tests: D-16 backward-compat + track-field parsing (Task 1, 8 tests), D-17 index-preservation + T-05-05-07 fallback-price regression + D-18 free-routing + T-05-05-03 eligibility cases (Task 2, 14 tests)

## Decisions Made
- Split the implementation into two atomic commits matching the plan's two tasks even though both tasks touch the same functions in the same file — reconstructed an intermediate Task-1-only state (3-tuple parsing/unpacking with no filtering yet) so the first commit's diff and test subset map exactly to Task 1's `<behavior>`/`<acceptance_criteria>`, then layered Task 2's filtering/eligibility logic on top for the second commit. No functional difference from doing it in one pass — purely a git-history clarity choice.
- Kept the T-05-05-03 eligibility check conditional on `tracks is not None` (skip `get_user` entirely for untracked options) rather than always re-resolving the track — matches the plan's own acceptance criterion that `grep -c "get_user" handlers/payment.py` only needs to be `>= 1`, and avoids adding a DB read to every payment tap for events that never use per-track pricing.

## Deviations from Plan

None - plan executed exactly as written. Every `<behavior>`, `<action>`, and `<done>` requirement in both tasks is implemented and test-covered; every acceptance-criteria grep/assertion/pytest command in the plan passes as specified, including the literal D-16/D-17 proof one-liners and the `options[0]`-absence source-inspection assertion.

## Issues Encountered

None. `python -m pytest tests/ -q` is fully green (252 passed, up from 230 at the start of this plan) — no regressions in `tests/test_payment_lc_requisites.py` or `tests/test_registration_phase4.py`, confirming the existing RusCo (non-party) payment configuration and its option indices are unaffected.

## Known Stubs

None. Every artifact this plan promised (`_parse_options` 3-tuple, `_visible_options`, track-filtered `start_payment_step`, the D-18 free-completion path, `process_payment_option`'s eligibility re-check, `approve_user`'s track threading) is fully wired end-to-end and exercised by tests — no placeholder values, no hardcoded stubs reaching the payment UI.

## Threat Flags

None. All six `mitigate`-disposition threats in the plan's threat register (T-05-05-01..05, 07) are addressed exactly as specified:
- T-05-05-01 (Tampering, index contract): `_visible_options` emits indices from `enumerate()` over the unfiltered list; `process_payment_option` keeps indexing that same list — proven by the D-17 index-preservation test.
- T-05-05-02 (Tampering, out-of-range/non-numeric index): the pre-existing bounds check at the top of `process_payment_option` was preserved unchanged, verified present after the tuple-unpack widening.
- T-05-05-03 (Elevation of Privilege, cross-track tariff purchase): closed with the new server-side re-check described above, test-covered for both the rejection and pass-through cases.
- T-05-05-07 (Tampering, single-option fallback selecting the unfiltered list's first entry): fallback now reads `visible[0]`; regression-tested with a non-zero-index party tariff.
- T-05-05-04 (Denial of Service, no tariff matches): D-18 routes to the free completion path.
- T-05-05-05 (Tampering, malformed `payment_options` line): non-integer prices and pipe-less lines still parse without raising, unchanged from Phase 4.

No new network endpoints, auth paths, or trust-boundary-crossing surface was introduced beyond what the plan's own threat model already covers.

## User Setup Required

None - no external service configuration required. Admins opt into per-track pricing purely by adding a third `|track` field to existing `payment_options` lines in the admin settings screen (already wired by plan 05-03) — no new `bot_settings` key, no schema change.

## Next Phase Readiness

- Plan 05-06 (Google Sheets routing / broadcast filter) can rely on `participant_type` being consistently resolved and threaded through the approval → payment path exactly as 05-04 and this plan established — no new track-resolution mechanism needed.
- ROADMAP SC#7 ("An approved party delegate sees only party tariffs and an approved full delegate only full tariffs") is now fully satisfied end-to-end: deep-link/fork entry (05-01/05-04) → track-aware approval (05-04) → track-filtered payment keyboard (this plan).
- No blockers. The full test suite (252 tests) is green with zero regressions against the pre-existing RusCo payment configuration.

---
*Phase: 05-participant-tracks-party-delegates*
*Completed: 2026-07-21*

## Self-Check: PASSED

All claimed files verified present (handlers/payment.py, handlers/registration.py, tests/test_payment_phase5.py, this SUMMARY.md). Both commits (cabc940, 84ff379) verified present in git log.
