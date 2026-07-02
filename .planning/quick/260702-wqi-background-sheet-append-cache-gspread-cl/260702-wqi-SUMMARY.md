---
quick_id: 260702-wqi
slug: background-sheet-append-cache-gspread-cl
date: 2026-07-02
status: complete
---

# Quick 260702-wqi — Summary

Three small reg/sheets fixes: kill the ~5s post-registration hang, cache the gspread client,
and sync the payment deadline into the reg question.

## A — background sheet append (`handlers/registration.py`)
Finalize awaited `append_to_sheet()` inline → the user blocked on a full Google round-trip
(auth + open + append, plus up to 3 retries with 5/15/30s sleeps) before getting the
completion message. Now the row is built inline (needs current settings) and the network
write is `asyncio.create_task(...)` — fire-and-forget, fail-soft. User sees completion instantly.

## B — cache gspread client (`services/sheets.py`)
`_get_sheet()` rebuilt the authorized client (`gspread.service_account`, token fetch) and
re-opened the spreadsheet on every call (~3 round-trips per registration). Now the worksheet
handle is cached at module level (google-auth refreshes the token on the cached client).
`_reset_sheet_cache()` drops the handle after any append/ensure failure to force a clean re-auth.

## C — sync payment deadline (`handlers/registration.py`)
The `payment_plan_date` question had the deadline **hardcoded** («Крайний срок 26.08.26») in
its default prompt and never read the `payment_deadline` setting. Now it pulls the date live
from `payment_deadline` (stored «ДД.ММ.ГГГГ ЧЧ:ММ», shows the date part). Admin prompt overrides
may embed `{deadline}` to position the date.

## Verification
- Import smoke (`main`, `services.sheets`, `handlers.registration`) → OK
- `python -m pytest tests/ -q -p no:asyncio` → **109 passed**

## Follow-ups (raised by user, not in this task)
- No admin notification when a payment receipt (чек) arrives.
- CSV export button is broken / stale.
