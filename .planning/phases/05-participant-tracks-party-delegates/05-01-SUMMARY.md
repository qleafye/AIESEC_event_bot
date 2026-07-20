---
phase: 05-participant-tracks-party-delegates
plan: 01
subsystem: database
tags: [aiosqlite, aiogram3, sqlite-migration, fsm]

# Dependency graph
requires:
  - phase: 04-universal-modules
    provides: payment module / consent module toggle posture, "new capability defaults OFF"
provides:
  - "users.participant_type / reg_started.participant_type additive columns"
  - "mark_reg_started(telegram_id, username, participant_type=None) + get_reg_started_track reader"
  - "add_user() round-trips participant_type"
  - "_is_party_track / _extract_party_track deep-link parsing (D-10)"
  - "party_enabled master gate in cmd_start with party_fallback_full opt-in callback (D-11a)"
  - "_start_registration_flow(participant_type=...) two-point track persistence (D-02)"
affects: [05-02, 05-03, 05-04, 05-05, 05-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Additive migration via _ensure_column, DEFAULT 'full' mirrors Phase-1 status precedent"
    - "COALESCE(excluded.x, table.x) guard for a repeat write that must not clobber an already-set value"
    - "Fail-soft try/except gate before registration-flow code (mirrors pre-selection gate)"
    - "callback.message.model_copy(update={'from_user': callback.from_user}) to fabricate a correctly-attributed Message from a CallbackQuery when reusing a message-shaped helper"

key-files:
  created:
    - tests/test_db_phase5.py
    - .planning/phases/05-participant-tracks-party-delegates/deferred-items.md
  modified:
    - database/db.py
    - handlers/registration.py

key-decisions:
  - "D-01: users.participant_type TEXT DEFAULT 'full' — additive, zero data loss for ~590 live rows"
  - "D-02: track persisted at TWO points (reg_started at flow start, users at finalize) because clear_reg_started deletes the reg_started row on completion"
  - "D-10: _extract_party_track matches ONLY the two literal tokens (party_over/party_noover) via a fixed dict — no prefix/startswith matching, so referrer_id and src_ tag extraction stay untouched"
  - "D-11a: master gate placed AFTER the already-registered branch in cmd_start so a returning delegate on a stale party link never sees the closed message; fail-soft, never silently reroutes into full"

patterns-established:
  - "Tri-state party-track resolution point (party_track truthy + party_enabled off = closed gate) — later plans (05-02..05-06) read participant_type off users/FSM data using this same vocabulary"

requirements-completed: [TRACK-01, TRACK-03]

# Metrics
duration: 7min
completed: 2026-07-20
---

# Phase 5 Plan 1: Party Track Foundation — DB migration + deep-link entry + two-point persistence Summary

**Party deep links (`?start=party_over` / `?start=party_noover`) now set `participant_type` on `users` via a two-point persistence chain (reg_started at flow start, users at finalize), gated by a fail-soft `party_enabled` master toggle that never silently reroutes a visitor into the pricier full track.**

## Performance

- **Duration:** ~7 min (first commit 19:55:34 → last commit 20:01:58, 2026-07-20)
- **Tasks:** 3/3 completed
- **Files modified:** 2 (database/db.py, handlers/registration.py)
- **Files created:** 1 test file + 1 deferred-items log

## Accomplishments
- `users.participant_type` (DEFAULT `'full'`) and `reg_started.participant_type` land via additive `_ensure_column` migrations — verified against both a fresh DB and a simulated pre-migration row, zero data loss.
- `mark_reg_started` widened with a `COALESCE(excluded.x, table.x)` guard so a bare repeat `/start` never erases a track set by an earlier deep-link tap; `get_reg_started_track` reader added.
- `add_user` round-trips `participant_type` at all three edit sites (INSERT column list / ON CONFLICT SET / VALUES tuple), defaulting to `'full'` when the key is absent.
- `_extract_party_track` / `_is_party_track` added — exact-match only against a fixed 2-entry map, so the existing referrer-id and `src_*` deep-link extractors stay mutually exclusive by construction (verified with a combined assertion test).
- `cmd_start` now extracts `party_track` alongside `referrer_id`/`source_tag`, enforces the `party_enabled` master gate **after** the already-registered branch (never intercepts a returning delegate), and recovers the track from `reg_started` on a bare repeat `/start` for a not-yet-registered user.
- `party_fallback_full` callback is the sole explicit opt-in out of the closed-party message; it starts the ordinary full flow and carries no user-supplied parameters.
- `_start_registration_flow` accepts `participant_type`, resolves it the same way as `referrer_id`/`source_tag` (fresh arg wins, else inherit from FSM), persists it to `reg_started` and FSM state before any question is asked.
- `finalize_registration` defaults `participant_type` to `'full'` before `add_user`, so every `users` row carries an explicit track regardless of entry path.

## Task Commits

Each task was committed atomically:

1. **Task 1: Phase 5 additive migrations + track persistence helpers in database/db.py** - `28c8c13` (feat)
2. **Task 2: Deep-link track extraction + party_enabled master gate in cmd_start (D-10, D-11a)** - `ef85a4d` (feat)
3. **Task 3: Persist the track at flow start and at finalization (D-02, D-01)** - `38c07a3` (feat)

## Files Created/Modified
- `database/db.py` - Phase 5 migration block, widened `mark_reg_started`, new `get_reg_started_track`, `add_user` round-trip
- `handlers/registration.py` - `_is_party_track`/`_extract_party_track`, `cmd_start` gate + track wiring, `party_fallback_full` callback, `_start_registration_flow`/`finalize_registration` track persistence
- `tests/test_db_phase5.py` (new) - 9 tests: migration data-loss safety, COALESCE guard, fresh-track-wins, unknown-id lookup, add_user round-trip + default, two-write-point contract
- `.planning/phases/05-participant-tracks-party-delegates/deferred-items.md` (new) - logs a pre-existing, out-of-scope environment gap (see Issues Encountered)

## Decisions Made
- Resolved a Claude's Discretion item from 05-CONTEXT.md: `reg_started.participant_type` is a new nullable TEXT column (not an alternate track-resolution mechanism) — matches the D-02 requirement with the least new surface area.
- `party_fallback_full`'s handler fabricates the `Message` passed to `_start_registration_flow` via `callback.message.model_copy(update={"from_user": callback.from_user})` rather than passing `callback.message` directly — see Deviations.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `party_fallback_full` callback would have attributed the flow to the bot, not the tapping user**
- **Found during:** Task 2 (party_fallback_full callback implementation)
- **Issue:** `callback.message` is the message the BOT sent (the one carrying the inline button) — its `from_user` is the bot's own `User` object, not the human who tapped. Passing `callback.message` straight into `_start_registration_flow` (which reads `message.from_user.id`/`message.from_user.username` for `mark_reg_started`) would have recorded the bot's telegram_id as "started registration," a real correctness bug on the only path out of the closed-party message.
- **Fix:** Built a corrected message via `callback.message.model_copy(update={"from_user": callback.from_user})`, confirmed (with a standalone aiogram/pydantic check) that `model_copy` preserves the private `_bot` attribute aiogram binds via `Message.as_(bot)`, so `.answer()` on the copy still resolves to the real bot instance and posts into the correct chat.
- **Files modified:** handlers/registration.py
- **Verification:** `python -m py_compile handlers/registration.py`; manual aiogram Message/model_copy probe confirming `_bot` and `from_user` both resolve correctly; full test suite (121 tests, apscheduler-blocked files excluded) green.
- **Committed in:** `ef85a4d` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Necessary for correctness of the only explicit opt-in path out of the closed-party gate (D-11a). No scope creep — same function, same commit as originally planned.

## Issues Encountered

**Pre-existing environment gap (out of scope, not fixed):** `python -m pytest tests/ -q` fails to *collect* 5 test files (`test_admin_phase2.py`, `test_broadcast_429_phase3.py`, `test_coins_phase1.py`, `test_nudge_phase3.py`, `test_scheduler_helpers_phase3.py`) with `ModuleNotFoundError: No module named 'apscheduler'` — `services/scheduler.py` imports `apscheduler.schedulers.asyncio.AsyncIOScheduler` at module load time, but `apscheduler`/`sqlalchemy` (both declared in `requirements.txt` since Phase 3) are not installed in the project's `.venv`. Confirmed via `git stash` that this is 100% pre-existing (identical 5 errors on a clean tree with none of this plan's changes applied) — out of scope per the executor's scope-boundary rule. All verification in this plan instead ran `pytest tests/ -q --ignore=<those 5 files>` → **121 passed, 0 regressions**. Logged to `.planning/phases/05-participant-tracks-party-delegates/deferred-items.md` with the exact `pip install` remediation command; not run here since it's an environment-setup action outside this task's file scope.

## Known Stubs

None. Every artifact this plan promised (`participant_type` migrations, `get_reg_started_track`, `_is_party_track`/`_extract_party_track`, the `party_enabled` gate, `party_fallback_full`, the two persistence write points) is fully wired end-to-end, not stubbed.

## Threat Flags

None. All five `mitigate`-disposition threats in the plan's threat register (T-05-01-01..05) are addressed exactly as specified: exact-match deep-link parsing, gate-before-flow ordering, parameter-free fallback callback, additive-only migration, and a fail-soft `get_reg_started_track` lookup. No new network endpoints, auth paths, or trust-boundary-crossing surface was introduced beyond what the threat model already covers.

## User Setup Required

None - no external service configuration required. (`apscheduler`/`sqlalchemy` venv installation is a pre-existing environment gap, not new setup introduced by this plan — see Issues Encountered.)

## Next Phase Readiness

- `participant_type` is now the single source of truth every later Phase 5 plan (05-02..05-06) reads: per-track question overrides (05-02+), per-track pricing (05-05), per-track sheet routing (05-06), and the admin broadcast filter (05-06/D-19) all build directly on this column and the `_is_party_track` predicate.
- `party_enabled` defaults `off` (key absent) — live full-registration flow is byte-identical to today until an admin explicitly turns the track on, matching the D-15 "new capability defaults OFF" posture carried from Phase 4.
- Blocker/action for the team (not blocking this plan): install `apscheduler==3.11.2` + `sqlalchemy>=2.0,<3.0` into `.venv` so the 5 currently-uncollectable test files run again — see `deferred-items.md`.

---
*Phase: 05-participant-tracks-party-delegates*
*Completed: 2026-07-20*
