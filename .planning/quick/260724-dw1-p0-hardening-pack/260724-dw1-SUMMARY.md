---
quick_id: 260724-dw1
status: complete
date: 2026-07-24
commits:
  - 67660c0  # fix: pin aiogram <4.0.0 + drop invalid compose version key
  - 7fbb4af  # feat: resume file-size guard in process_resume
  - a53a6ad  # feat: admin alert on exhausted Sheets append retries
tests: "tests/test_registration_phase1.py (15/15) + tests/test_sheets_admin_alert.py (8/8 new) + full suite 359/359 pass"
requirements-completed: [P0-aiogram-pin, P0-resume-size-guard, P0-sheets-fail-alert, P0-compose-version]
---

# Quick Task 260724-dw1 — P0 Hardening Pack

**Four low-risk P0 fixes from the v1.0 milestone audit: aiogram upper-bound pin, resume file-size guard, Sheets exhausted-retry admin alert, invalid docker-compose `version` key removed.**

## Performance

- **Duration:** ~25 min
- **Tasks:** 3/3 completed
- **Files modified:** 6 (+1 new test file)

## Accomplishments

1. **T-dw1-03 (aiogram pin):** `requirements.txt` bounds `aiogram>=3.0.0,<4.0.0` — a fresh install can no longer pull a breaking 4.x major. Compose `version: 'version'` (invalid) removed; `docker-compose.yml` parses cleanly under compose v2 with the `bot` service intact.
2. **T-dw1-01 (resume size guard):** `handlers/registration.py::process_resume` now rejects oversized documents (>10 MB) with a RU error message BEFORE `state.update_data`/`_advance` — no file_id stored, no later Nextcloud download/upload triggered. `_resume_too_large` mirrors `handlers/payment.py::_receipt_too_large` exactly (0/None passes, at-limit passes, over-limit rejected), replicated locally per the plan (no cross-module import).
3. **T-dw1-02 (Sheets exhausted-retry alert):** `services/sheets.py` gained `set_alert_bot()` injection + `_alert_admins_sheet_failure()`, mirroring `services/scheduler.py::allowlist_refresh_job`'s per-admin send loop. Both `append_to_sheet` and `append_to_named_sheet` call it after their retry loops exhaust (right after the existing `logger.error`). Fully fail-soft: unset bot logs once and returns; a per-admin `send_message` failure is swallowed with the remaining admins still attempted; never raises into the caller. `main.py` wires `sheets_service.set_alert_bot(bot)` right after `init_scheduler(bot)`, before polling starts.

## Task Commits

1. **Task 1: Pin aiogram upper bound + drop invalid compose version key** - `67660c0` (fix)
2. **Task 2: Resume file-size guard in process_resume + regression tests** - `7fbb4af` (feat)
3. **Task 3: Admin alert on exhausted Sheets retries + tests** - `a53a6ad` (feat)

## Files Created/Modified

- `requirements.txt` - aiogram upper bound `<4.0.0` (no other dep line touched)
- `docker-compose.yml` - invalid top-level `version: 'version'` key removed; `services:`/`bot:` byte-identical otherwise
- `handlers/registration.py` - `_RESUME_MAX_BYTES`/`_resume_too_large` helpers + size check in `process_resume` before FSM advance
- `tests/test_registration_phase1.py` - +5 tests for `_resume_too_large` (none/zero/at-limit/over-limit/small)
- `services/sheets.py` - `set_alert_bot`, `_alert_admins_sheet_failure`; called from both `append_to_sheet` and `append_to_named_sheet` after retry exhaustion
- `main.py` - `sheets_service.set_alert_bot(bot)` wired at startup alongside `init_scheduler(bot)`
- `tests/test_sheets_admin_alert.py` (new) - 8 tests: alert fires with correct admin ids on exhaustion (both append paths), no alert on success, fail-soft when bot unset, fail-soft when `send_message` raises (other admin still attempted)

## Decisions Made

- Resume size cap replicated locally as `_RESUME_MAX_BYTES` in `handlers/registration.py` rather than imported from `handlers/payment.py`, per plan instruction — avoids a cross-module import risk and keeps the two caps independently tunable even though both currently equal 10 MB.
- `_alert_admins_sheet_failure` wraps its entire body in try/except (not just the per-admin send) so a bug in the helper itself (e.g. a bad `config.ADMIN_IDS` value) can never propagate into the Sheets append retry path — matches the plan's "fully fail-soft" requirement literally.
- Warn-once (`_alert_bot_warned` flag) added for the unset-bot case so repeated exhausted appends before startup wiring completes don't spam the log — a small addition beyond the plan's literal text but consistent with the codebase's existing warn-once/fail-soft idioms elsewhere in `sheets.py` (e.g. `ensure_sheet_header`).

## Deviations from Plan

None - plan executed exactly as written. `pyyaml` was installed into the venv only to run the plan's own YAML-parsing verification command for Task 1 (not added to `requirements.txt`, not a runtime dependency of the bot).

## Issues Encountered

None.

## Test Results

- `tests/test_registration_phase1.py` — 15/15 pass (10 pre-existing + 5 new size-guard tests)
- `tests/test_sheets_admin_alert.py` — 8/8 pass (new file)
- Full suite: `pytest -q` — **359/359 pass**, no regressions
- `docker-compose.yml` parses under `yaml.safe_load` with no `version` key and `bot` in `services`

## User Setup Required

None - no external service configuration required. All four fixes are code/config-only.

## Next Phase Readiness

- All four P0 audit findings (T-dw1-01..04 in the plan's threat register) closed.
- `NEXTCLOUD_VERIFY_TLS` default (self-signed-cert deployment, T-dw1-04) intentionally left untouched — tracked separately per plan scope.
- No blockers for subsequent work.

---
*Quick task: 260724-dw1*
*Completed: 2026-07-24*

## Self-Check: PASSED

- FOUND: commit 67660c0
- FOUND: commit 7fbb4af
- FOUND: commit a53a6ad
- FOUND: tests/test_sheets_admin_alert.py
- FOUND: .planning/quick/260724-dw1-p0-hardening-pack/260724-dw1-SUMMARY.md
- FOUND: `_resume_too_large` in handlers/registration.py
- FOUND: `_alert_admins_sheet_failure` in services/sheets.py
- FOUND: `set_alert_bot` in main.py
