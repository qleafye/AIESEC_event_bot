---
phase: quick-260713-jgi
plan: 01
subsystem: security
tags: [html-escape, csv-injection, aiogram, xss-adjacent, cwe-1236]

# Dependency graph
requires:
  - phase: n/a (quick task)
    provides: n/a
provides:
  - HTML-escaping at every named user/admin interpolation site feeding a parse_mode=HTML send
  - Fail-soft plain-text fallback for consent card sends
  - _csv_safe formula-injection neutralizer applied in export_users_csv (CR-6)
affects: [admin-panel, registration-flow, user-actions, csv-export]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "html(_module).escape(str(value)) at every interpolation of a user/admin-controlled
       string into an HTML-parse-mode Telegram send; guard None so it doesn't render as
       the literal string 'None'."
    - "_csv_safe: prefix a leading single-quote onto any str cell starting with
       = + - @ TAB CR before writing to CSV, so spreadsheet apps treat it as text
       (CWE-1236). Non-str cells and headers pass through unchanged."

key-files:
  created:
    - tests/test_csv_injection.py
  modified:
    - handlers/admin.py
    - handlers/user_actions.py
    - handlers/registration.py
    - database/db.py

key-decisions:
  - "Escape only the interpolated values, never surrounding literal text/tags/emoji — zero wording/formatting changes."
  - "registration.py consent caption: moved html.escape(...) to wrap the _prompt(...) return value (was wrapping only the fallback label) so it also covers the admin reg_prompt_consent_<key> override and avoids double-escaping the default."
  - "Consent sends (answer_document / answer) wrapped try/except with a parse_mode=None plain-text resend on failure — fail-soft per file convention."
  - "CSV neutralizer is a single centralized _csv_safe applied per-cell in export_users_csv (not scattered in admin.py CSV writers), so both /export and the rebuild-sheet CSV path get it for free."

requirements-completed: [CR-2, CR-3, CR-4, CR-5, C-WR-02, A-WR-03, CR-6]

# Metrics
duration: 15min
completed: 2026-07-13
---

# Phase quick-260713-jgi: Security Batch B1a — HTML/CSV Injection Fixes Summary

**Closed 7 CRITICAL findings from the all-phases code review: HTML-escaped every named user/admin interpolation feeding a parse_mode=HTML Telegram send (CR-2/3/4/5, C-WR-02, A-WR-03), and added a centralized `_csv_safe` formula-injection neutralizer to the CSV export path (CR-6).**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-07-13T20:05:00Z (approx.)
- **Completed:** 2026-07-13T20:09:20Z
- **Tasks:** 2
- **Files modified:** 5 (4 modified, 1 created)

## Accomplishments
- `/find`, `/stats`, `admin_stats`, `admin_source_stats` in `handlers/admin.py` now HTML-escape `full_name`/`username`/`email`/`uni`/`source` — a registrant with `<`/`>`/`&` in any of these fields can no longer break the admin message or inject markup.
- `handlers/user_actions.py`: referral names in "Мои приглашённые", admin-set `event_date`/`event_time`/`place_name`/`place_address`, and `program_caption`/`speakers_caption` are now escaped (None-guarded so a missing caption stays falsy, not the literal string "None").
- `handlers/registration.py`: consent caption escape moved outward to cover both the default label AND the admin `reg_prompt_consent_<key>` override; consent sends (`answer_document`/`answer`) are now fail-soft with a plain-text (`parse_mode=None`) resend on failure.
- `database/db.py`: new `_csv_safe(value)` helper neutralizes CSV/Excel formula injection (CWE-1236) by prefixing a single quote onto any string cell starting with `= + - @ \t \r`; applied per-cell in `export_users_csv` before the `(headers, rows)` tuple is returned. Non-string cells (int/None) and headers pass through unchanged. Both admin CSV export call sites (`handlers/admin.py:1063`, `:1183`) consume the neutralized rows automatically — no changes needed there.
- New pure-function test suite `tests/test_csv_injection.py` (10 tests) covers all `_csv_safe` cases from the plan's `<behavior>` block.

## Task Commits

Each task was committed atomically:

1. **Task 1: HTML-escape all named user/admin interpolation sites** - `9f598b4` (fix)
2. **Task 2: CSV formula-injection neutralizer + test (CR-6)** - `16c458b` (test)

**Plan metadata:** committed separately by orchestrator (docs: complete plan)

## Files Created/Modified
- `handlers/admin.py` - escape `full_name`/`username`/`email` in `/find`; escape `uni` in both `cmd_stats` and `show_admin_stats` top-university loops; escape `source` in `show_admin_source_stats`
- `handlers/user_actions.py` - escape referral names, event date/time/place fields, program/speakers captions (None-guarded)
- `handlers/registration.py` - escape moved outward to cover admin prompt override; consent sends now fail-soft to plain text
- `database/db.py` - added `_CSV_INJECTION_PREFIXES` + `_csv_safe(value)`; applied per-cell in `export_users_csv`
- `tests/test_csv_injection.py` - new pure-function unit test (10 cases) for `_csv_safe`

## Decisions Made
- Escape only the interpolated values, never touch surrounding literal text/tags/emoji — verified via full `git diff` review (see Deviations below), confirms zero wording/formatting changes.
- `registration.py`: relocated the `html.escape(...)` call to wrap `await _prompt(...)`'s return value rather than only the fallback default, so an admin-set `reg_prompt_consent_<key>` override is escaped too (previously it would have bypassed escaping entirely).
- Consent-card sends wrapped in try/except with a `parse_mode=None` plain-text fallback, matching the file's existing fail-soft convention (Rule 2 — missing critical resilience for an HTML-parse failure path).
- `_csv_safe` kept as a single centralized sanitizer inside `database/db.py::export_users_csv` rather than scattered inline checks in the two `handlers/admin.py` CSV writer call sites, per the plan's explicit instruction.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Missing local `.env` blocked test collection**
- **Found during:** Task 1 verification (`python -m pytest`)
- **Issue:** `config.py` requires `BOT_TOKEN` and `ADMIN_IDS` via `.env` (pydantic-settings `Settings()`); no `.env` existed in this worktree (gitignored, not present in the main repo working copy either), so every test module failed at collection with a `pydantic_core.ValidationError`.
- **Fix:** Created a local `.env` with dummy `BOT_TOKEN`/`ADMIN_IDS` values purely to unblock local test execution. `.env` is gitignored (`git check-ignore` confirmed) — never staged or committed, no secrets involved.
- **Files modified:** `.env` (untracked, gitignored — not part of the commit)
- **Verification:** `python -m pytest tests/ -q -p no:asyncio` → 121 passed (baseline) before Task 1 changes were even added; confirmed the fix was environmental, not code-related.
- **Committed in:** N/A (gitignored, no commit)

---

**Total deviations:** 1 auto-fixed (1 blocking — test environment setup, no code impact)
**Impact on plan:** No scope creep. The `.env` fix was purely local test-environment plumbing required to run the plan's own verification steps; it touches no tracked file and is invisible to the repo.

## Issues Encountered
- Worktree HEAD was initially one commit behind the plan file's commit (`33ca7d8`, "docs(260713-jgi): pre-dispatch plan for B1a security fixes") — the pre-execution `<worktree_branch_check>` correctly detected the mismatch via `git merge-base` and reset the worktree to the expected base commit before any edits, per protocol.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All 7 CRITICAL findings in this batch (CR-2, CR-3, CR-4, CR-5, C-WR-02, A-WR-03, CR-6) are closed.
- Full test suite: 131 passed (121 prior + 10 new `test_csv_injection.py` cases), `-p no:asyncio` (project convention — pytest-asyncio plugin broken in this env).
- `git diff` against the plan's base commit touches exactly the 5 files listed in the plan's `files_modified` frontmatter — no unplanned files.
- No blockers for subsequent security-batch quick tasks (if any B1b/B2 batches follow from the same code review).

---
*Phase: quick-260713-jgi*
*Completed: 2026-07-13*

## Self-Check: PASSED

All created/modified files found on disk: `tests/test_csv_injection.py`, `handlers/admin.py`,
`handlers/user_actions.py`, `handlers/registration.py`, `database/db.py`, this SUMMARY.md.
Both task commits found in git log: `9f598b4` (Task 1), `16c458b` (Task 2).
