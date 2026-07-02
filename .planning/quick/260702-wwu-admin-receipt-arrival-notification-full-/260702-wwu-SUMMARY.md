---
quick_id: 260702-wwu
slug: admin-receipt-arrival-notification-full-
date: 2026-07-02
status: complete
---

# Quick 260702-wwu — Summary

## Receipt-arrival admin notification (`handlers/payment.py`)
`_finalize_receipt` set status `receipt_sent` but never pinged admins — receipts sat unseen in
the «🧾 Чеки» review queue (no push, no receipt-reminder loop). Added a fail-soft instant
notify to every `config.ADMIN_IDS`: «🧾 Новый чек оплаты от {ФИО}. Проверь: /admin → 🧾 Чеки».
Added `from config import config`. Notify errors never break the user's receipt confirmation.

## Full readable CSV export (`database/db.py`)
`export_users_csv` did `SELECT *` with raw English column names and **dropped phone**. Per
user choice (full audit/backup dump): now returns every column including phone + service
fields, mapped through `CSV_HEADER_LABELS` to readable RU headers. Unmapped columns keep their
raw name so a newly-added column still exports. (Note: phone is now included — admin-only export.)

## Verification
- Import smoke (`handlers.payment`, `main`) → OK
- CSV: 56 columns, RU headers («ФИО», «Телефон» present), phone value present
- `python -m pytest tests/ -q -p no:asyncio` → **109 passed**
