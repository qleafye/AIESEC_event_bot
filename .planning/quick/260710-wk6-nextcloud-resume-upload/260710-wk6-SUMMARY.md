---
phase: quick-260710-wk6
plan: 01
subsystem: registration
tags: [nextcloud, webdav, resume, google-sheets, fail-soft]
requires: [resume_file_id FSM field, services.sheets.append_to_sheet, add_user]
provides: [services.nextcloud.upload_resume, users.resume_url, "«Резюме (ссылка)» sheet column"]
affects: [config.py, database/db.py, handlers/registration.py]
tech-stack:
  added: [aiohttp WebDAV PUT, Nextcloud OCS v2 Share API]
  patterns: [fail-soft external I/O, SecretStr for credentials, _ensure_column migration, COALESCE re-registration guard]
key-files:
  created: [services/nextcloud.py, tests/test_nextcloud_wk6.py]
  modified: [config.py, database/db.py, handlers/registration.py]
decisions:
  - "Upload runs BEFORE add_user (not after, as the plan literally placed it) so resume_url persists to DB per must_have truth #1 — still before active_sheet_row, so the link also lands in the sheet."
  - "resume_file_name kept in FSM only (no DB/sheet column) purely to preserve the file extension for the Nextcloud upload filename (PDF preview)."
metrics:
  duration: ~20m
  completed: 2026-07-10
---

# Quick 260710-wk6: Nextcloud Resume Upload Summary

Self-hosted Nextcloud integration: a delegate finishing registration with a FILE resume gets it uploaded via WebDAV and a password-protected public OCS share link stored in `users.resume_url` and the new «Резюме (ссылка)» Google Sheet column — fully fail-soft, so a Nextcloud outage never blocks or breaks registration, and the share password never reaches the DB or sheet.

## What Was Built

- **config.py** — `NEXTCLOUD_BASE_URL/WEBDAV_URL/USER/APP_PASS/FOLDER/SHARE_PASSWORD/VERIFY_TLS` Settings fields. All empty = feature off. Credentials are `SecretStr`.
- **database/db.py** — `resume_url TEXT` column via `_ensure_column` (additive, idempotent); wired into `add_user` INSERT + values tuple + `resume_url=COALESCE(excluded.resume_url, users.resume_url)` ON CONFLICT guard; CSV label `"resume_url": "Резюме (ссылка)"`.
- **services/nextcloud.py** — `async upload_resume(bot, file_id, filename) -> str | None`. Entire body in try/except; returns None on empty config / any error / timeout, never raises. Steps: download bytes → collision-safe `uuid8_<sanitized-name>` (extension preserved) → WebDAV PUT (accept 200/201/204) → OCS v2 public share (shareType=3, read-only, optional password) → extract `ocs.data.url`. `ssl=False` for self-signed certs when `VERIFY_TLS` is off. 15s aiohttp total timeout. Password read via `get_secret_value()`, never logged or returned.
- **handlers/registration.py** — import `upload_resume`; `process_resume` now stores `resume_file_name` in FSM (extension for preview); new `("Резюме (ссылка)", "reg_q_resume", …)` SHEET_COLUMNS entry (auto-integrates into headers/row/map); finalize runs the fail-soft upload bounded by `asyncio.wait_for(..., timeout=20)` and sets `data["resume_url"]` on success.
- **tests/test_nextcloud_wk6.py** — 5 tests: column added, sheet column present + maps URL, disabled config → None, fail-soft on raising bot → None, COALESCE preserves resume_url on re-registration.

## Verification

- `pytest tests/test_nextcloud_wk6.py tests/test_db_phase4.py tests/test_registration_phase4.py -q` → **33 passed**.
- Import check: `import handlers.registration` OK (no import-cycle / syntax issues).
- Password-leak grep: no `SHARE_PASSWORD` reference anywhere in `handlers/` or `database/`.

## Deviations from Plan

### 1. [Rule 1 - Correctness] Upload moved before `add_user` instead of after
- **Found during:** Task 3
- **Issue:** The plan literally placed the upload *between* `add_user` and `active_sheet_row`. But `add_user` is what persists `resume_url` to the DB, so uploading after it would leave `users.resume_url` empty — violating must_have truth #1 ("stored in DB + sheet").
- **Fix:** Placed the fail-soft upload block right after the `resume_url` setdefault and **before** `add_user`. This still satisfies the key_link "before active_sheet_row" while also persisting the link to the DB.
- **Files modified:** handlers/registration.py
- **Commit:** 37c3297

### 2. [Extra instruction] resume_file_name stored in FSM
- Per the executor's extra instruction: `process_resume` now stores `message.document.file_name` in FSM (non-persisted, no DB/sheet column) so the Nextcloud filename keeps the real `.pdf`/`.docx` extension for preview. Finalize derives `fname = data.get("resume_file_name") or "resume"`.
- **Commit:** 37c3297

## Environment Notes (not committed)

- The project `.venv` lacked `pytest`; installed it into `.venv` to run the plan's verify commands.
- Created a gitignored `.env` in the worktree (dummy `BOT_TOKEN`/`ADMIN_IDS`) so `config.Settings()` imports during tests. Confirmed gitignored — not committed.

## Known Stubs

None. `resume_url` is fully wired end-to-end (upload → DB → sheet). When Nextcloud is unconfigured the column simply stays empty, which is the intended fail-soft behavior, not a stub.

## Commits

- `766cc9e` feat(quick-260710-wk6): NEXTCLOUD_* config + resume_url column/label/persistence
- `cae7b38` feat(quick-260710-wk6): services/nextcloud.py fail-soft upload_resume
- `37c3297` feat(quick-260710-wk6): finalize wiring + «Резюме (ссылка)» sheet column + tests

## Self-Check: PASSED

- FOUND: services/nextcloud.py
- FOUND: tests/test_nextcloud_wk6.py
- FOUND commits: 766cc9e, cae7b38, 37c3297
