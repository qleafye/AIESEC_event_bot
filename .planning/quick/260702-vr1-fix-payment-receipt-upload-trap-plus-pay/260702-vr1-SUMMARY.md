---
quick_id: 260702-vr1
slug: fix-payment-receipt-upload-trap-plus-pay
date: 2026-07-02
status: complete
---

# Quick 260702-vr1 — Summary

## What changed

**Bug fixed:** users stuck in the payment window (`Registration.receipt_upload`) could
not escape — the catch-all `handlers/payment.py:process_receipt_invalid` matched every
message (including `/start`), and `payment.router` is included before `registration.router`,
so `cmd_start` (`StateFilter("*")`) never ran.

### `handlers/payment.py`
- Catch-all now filtered with `~F.text.startswith("/")` → `/start` and `/cancel` fall
  through to their real handlers. Copy updated to point the user at `/start`.
- Added `should_offer_receipt_upload(telegram_id)` — the DB-status gate driving the menu
  button (`payment_enabled=on` AND `payment_status ∈ {not_paid, overdue}` AND non-empty
  resolved requisites). Survives the MemoryStorage FSM reset on restart.
- Added `⏭ Оплачу позже` inline button (`_PAY_LATER_BTN`) on both the option picker and
  the requisites message, plus `process_pay_later` callback (clears state → main menu).

### `keyboards/builders.py`
- `get_main_menu_kb(telegram_id: int | None = None)` — appends `💳 Загрузить чек` when the
  user still owes a receipt. Lazy import of the predicate (avoids circular import), fail-soft.

### `handlers/user_actions.py`
- Handler for `💳 Загрузить чек` → `ensure_registered` → predicate guard → `start_payment_step`.
- All 4 `get_main_menu_kb()` call sites now pass `message.from_user.id`.

### `handlers/registration.py`, `handlers/admin.py`
- Remaining `get_main_menu_kb()` call sites pass the user's id (welcome, admin_rereg skip,
  finalize, receipt-confirm).

## User-facing flow
Approved unpaid user → sees requisites with `⏭ Оплачу позже`. Taps it → back to menu, which
now shows `💳 Загрузить чек`. Pays whenever → taps the button → uploads PDF/photo. Works after
a bot restart (gated on DB, not FSM). Button disappears once `receipt_sent`/`paid`.

## Verification
- `python -m pytest tests/ -q -p no:asyncio` → **107 passed**
- Import smoke (`import main` + all touched modules) → OK

## Notes / follow-ups
- Free/single-option participants (no requisites) keep `payment_status=not_paid` but the
  predicate's requisites check keeps the button hidden for them.
- Not done here (separate task): forum/conference **preset** layer over the `reg_q_*` toggles.
