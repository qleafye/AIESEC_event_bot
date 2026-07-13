---
phase: quick-260713-i4p
plan: 01
subsystem: payments
tags: [aiogram, telegram, payment-flow, html-escape, reply-keyboard]

# Dependency graph
requires:
  - phase: quick-260709-mog
    provides: payment reminders, payment_status field, per-LC requisites setting
provides:
  - Persistent menu button renamed «💳 Загрузить чек» → «💳 Оплата» (keyboard + handler filter kept in exact sync)
  - Pay-later confirmation message now shows resolved requisites (per-LC or shared), HTML-escaped
  - Multi-option payment picker now shows resolved requisites alongside the option buttons, HTML-escaped
  - Repo-wide sweep confirming zero remaining "Загрузить чек" references in .py source
affects: [payments, user-onboarding]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Fail-soft optional-block message building: build a list of message parts, append a block only when the underlying setting is truthy after .strip(), then join — mirrors the existing _show_payment_details pattern."

key-files:
  created: []
  modified:
    - keyboards/builders.py
    - handlers/user_actions.py
    - handlers/payment.py
    - services/scheduler.py

key-decisions:
  - "Reused _resolve_requisites() unchanged (per-LC then shared fallback) in both new call sites rather than duplicating requisites-lookup logic."
  - "Added parse_mode=\"HTML\" to both process_pay_later and the multi-option send in start_payment_step, matching the html.escape(requisites) pattern already used in _show_payment_details, to guard against admin-entered & / < in requisites breaking HTML parsing."

patterns-established: []

requirements-completed: []

# Metrics
duration: 5min
completed: 2026-07-13
---

# Quick Task 260713-i4p: Payment UX Fixes — Rename Button, Surface Requisites Summary

**Renamed the persistent "upload receipt" menu button to «💳 Оплата» (kept keyboard + handler filter byte-identical) and surfaced payment requisites in both the pay-later confirmation and the multi-option picker, so unpaid users can actually find where/how to pay instead of hitting a receipt-upload-only button.**

## Performance

- **Duration:** ~5 min (03254e6 → 4cfdffc)
- **Started:** 2026-07-13T13:08:03+03:00
- **Completed:** 2026-07-13T13:12:49+03:00
- **Tasks:** 2 completed
- **Files modified:** 4

## Accomplishments
- Menu button label and its handler filter renamed from «💳 Загрузить чек» to «💳 Оплата» in lockstep (kept `keyboards/builders.py` and `handlers/user_actions.py` byte-identical, as aiogram matches reply-keyboard taps by exact string equality)
- `process_pay_later` now resolves and shows the user's requisites (per-LC or shared fallback via `_resolve_requisites`) directly in the "Оплачу позже" confirmation, HTML-escaped, before pointing to the renamed menu button
- `start_payment_step`'s multi-option branch now shows requisites alongside the option picker (before the user taps anything), HTML-escaped
- Swept the whole repo for remaining `.py` references to the old label — found and fixed one additional stale docstring reference in `handlers/payment.py` (`should_offer_receipt_upload`) not explicitly called out in the plan's task list but caught by the plan's own sweep instruction

## Task Commits

Each task was committed atomically:

1. **Task 1: Rename menu button label and its handler filter to «💳 Оплата»** - `700c43c` (feat)
2. **Task 2: Show requisites in pay-later message and multi-option picker; sweep remaining stale label references** - `4cfdffc` (feat)

**Plan metadata:** commit pending (docs commit handled by orchestrator)

## Files Created/Modified
- `keyboards/builders.py` - Menu button text literal changed from "💳 Загрузить чек" to "💳 Оплата"
- `handlers/user_actions.py` - `@router.message(F.text == ...)` filter on `upload_receipt_entry` updated to match the new button text exactly
- `handlers/payment.py` - `process_pay_later` now resolves and shows requisites (HTML-escaped, parse_mode="HTML"); `start_payment_step` multi-option branch now resolves and shows requisites (HTML-escaped, parse_mode="HTML"); stale docstring reference to old button label in `should_offer_receipt_upload` updated
- `services/scheduler.py` - Overdue-payment reminder default text updated to reference «💳 Оплата» instead of «💳 Загрузить чек»

## Decisions Made
- Reused `_resolve_requisites()` as-is at both new call sites (no signature change) — keeps `tests/test_payment_lc_requisites.py` passing unmodified, per plan constraint.
- Added `parse_mode="HTML"` to both new sends since requisites text is now interpolated via `html.escape()`; mirrors the existing `_show_payment_details` escaping pattern (CR-01 threat mitigation, same as plan's threat register T-q260713-01/02).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug/Consistency] Fixed one additional stale docstring reference the plan's task text didn't explicitly quote**
- **Found during:** Task 2 (repo sweep step)
- **Issue:** `handlers/payment.py`'s `should_offer_receipt_upload` docstring still read `persistent '💳 Загрузить чек' menu button` — not explicitly named in the plan's task action text (which called out "the module docstring reference... line ~1-8"), but caught by the plan's own explicit sweep instruction ("grep the whole repo... for any remaining literal `Загрузить чек`... update each").
- **Fix:** Updated the docstring to `persistent '💳 Оплата' menu button`.
- **Files modified:** handlers/payment.py
- **Verification:** `grep -rn "Загрузить чек" handlers/ services/ keyboards/` returns no matches; full test suite passes.
- **Committed in:** 4cfdffc (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 Rule 1 — consistency sweep, explicitly anticipated by the plan's own sweep instruction)
**Impact on plan:** No scope creep — this was mandated by the plan's Task 2 sweep step, just not itemized as a separate bullet.

## Issues Encountered
- Local test run required `BOT_TOKEN` and `ADMIN_IDS` env vars (no `.env` present in the worktree; `config.py` uses `pydantic-settings` which raises `ValidationError` without them). Not a code issue — set both env vars inline for the pytest invocation only, no repo files changed. This is a pre-existing test-environment gap, out of this plan's scope (logged here for visibility, not fixed).

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Payment UX fix is self-contained and ships cleanly; no follow-on work required.
- `tests/test_payment_lc_requisites.py` (4 tests) and the full suite (121 tests) pass unmodified — `_resolve_requisites` signature/behavior untouched.
- Pre-existing blocker from STATE.md unaffected: "Pre-Phase 4: Confirm payment cancellation scope" remains open, unrelated to this quick task.

---
*Phase: quick-260713-i4p*
*Completed: 2026-07-13*

## Self-Check: PASSED

All modified files confirmed present (keyboards/builders.py, handlers/user_actions.py, handlers/payment.py, services/scheduler.py, this SUMMARY.md). Both task commits confirmed in `git log --oneline --all` (700c43c, 4cfdffc).
