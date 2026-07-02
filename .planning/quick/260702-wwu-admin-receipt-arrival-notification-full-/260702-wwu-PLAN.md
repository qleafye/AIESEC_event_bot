---
quick_id: 260702-wwu
slug: admin-receipt-arrival-notification-full-
date: 2026-07-02
status: complete
---

# Quick 260702-wwu — Admin receipt notification + full readable CSV

## Tasks
1. `handlers/payment.py`: notify admins (fail-soft, instant) in `_finalize_receipt` when a
   receipt arrives; add `config` import.
2. `database/db.py`: `export_users_csv` → full dump (all columns incl. phone) with RU headers
   via `CSV_HEADER_LABELS`; unmapped columns keep raw name.

## Verify
- Import smoke; CSV has RU headers + phone; `pytest -q -p no:asyncio` green.
