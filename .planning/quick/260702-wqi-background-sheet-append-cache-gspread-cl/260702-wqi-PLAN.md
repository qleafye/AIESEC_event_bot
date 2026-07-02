---
quick_id: 260702-wqi
slug: background-sheet-append-cache-gspread-cl
date: 2026-07-02
status: complete
---

# Quick 260702-wqi — Background sheet append + cache gspread + sync payment deadline

## Tasks
1. A: fire-and-forget the finalize Google Sheet append (`asyncio.create_task`) so the user
   is not blocked ~5s on the network write + retries.
2. B: cache the gspread client/worksheet in `services/sheets.py`; reset cache on failure.
3. C: build the `payment_plan_date` question deadline from the `payment_deadline` setting
   (remove hardcoded date); support `{deadline}` placeholder in admin overrides.

## Verify
- Import smoke + `python -m pytest tests/ -q -p no:asyncio` green.
