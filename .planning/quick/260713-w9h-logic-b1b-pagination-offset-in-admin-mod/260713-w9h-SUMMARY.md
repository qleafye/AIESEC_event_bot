---
status: complete
phase: quick-260713-w9h
requirements: [CR-1, CR-7, CR-8, CR-9]
files_modified:
  - handlers/admin.py
  - database/db.py
  - handlers/registration.py
  - main.py
  - tests/test_review_b1b.py
commits:
  - 543e41b  # CR-1 pagination (executor, before interruption)
  - 4a9aed1  # CR-7/8/9 (completed inline after executor API failure)
tests: "140 passed (was 131; +9 across CR-1/7/8/9)"
---

# B1b — Logic Criticals SUMMARY

Fixed the four LOGIC criticals from the all-phases review (CR-2..CR-6 were done in B1a).

## Execution note
The gsd-executor subagent completed **CR-1** and committed it (`543e41b`), then died mid-run
on an API/billing error ("org disabled Claude subscription access for Claude Code"). CR-1 was
salvaged by merging its clean atomic commit; the interrupted partial work was discarded.
**CR-7/8/9 were then implemented inline on the main thread** (subagents unavailable) and
committed as `4a9aed1`. Full suite green throughout.

## What changed

- **CR-1 — queue paging** (`database/db.py`, `handlers/admin.py`): `get_receipt_pending_users`
  gained an `offset` param (mirrors `get_pending_users`); both tinder renderers page forward
  by offset instead of re-fetching the oldest 50, so skipping past 50 no longer false-reports
  «нет заявок». <50-item behavior unchanged.
- **CR-7 — consent bypass** (`handlers/registration.py`): `process_consent_accept` now gates on
  `_consent_key_matches(tapped, active _consent_key)` — a stale re-tap of an earlier consent
  card records nothing and does not advance; the accepted card's inline keyboard is cleared.
- **CR-8 — crash + silent drops** (`handlers/registration.py`, `main.py`): `_extract_referrer_id`
  and new `_parse_age` require `isascii() and isdigit()`, so `²`/`①`/fullwidth digits yield
  None instead of crashing `int()`. A global `@dp.errors()` handler now logs the exception +
  offending update (was: unhandled errors silently dropped).
- **CR-9 — sheet misalignment** (`handlers/registration.py`, `main.py`, `handlers/admin.py`):
  `active_sheet_row` projects onto a frozen header snapshot (`set_sheet_schema`/`get_sheet_schema`
  in `bot_settings`), persisted at startup header write and on admin rebuild. A mid-event
  question toggle no longer shifts row columns. Missing-snapshot deployments fall back to live
  headers (zero migration risk — KV key only, no schema change).

## Tests
`tests/test_review_b1b.py`: CR-1 paging (offset past 50, no overlap, default unchanged),
CR-8 referrer/age Unicode rejection + valid/guard cases, CR-7 `_consent_key_matches` truth
table, CR-9 frozen-schema width-after-toggle + live fallback. **140 passed** (`-p no:asyncio`).

## Manual UAT pending (behavioral, not fully unit-testable)
1. **CR-1 queue paging** — with 50+ pending applications, /admin → «📋 Заявки», tap «⏭ Пропустить»
   through all 50 visible cards; confirm the queue advances to item 51+ (correct position N),
   not «✅ Заявок нет.». Repeat for «🧾 Чеки» with 50+ receipts.
2. **CR-7 consent order** — enable ≥2 consents; in registration accept consent #1 → #2 appears;
   scroll up and re-tap #1's button; confirm nothing advances and #2 must still be accepted;
   confirm accepted buttons no longer respond.
