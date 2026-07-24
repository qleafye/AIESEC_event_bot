---
phase: 04-universal-modules
reviewed: 2026-06-30T00:00:00Z
depth: standard
files_reviewed: 8
files_reviewed_list:
  - database/db.py
  - handlers/admin.py
  - handlers/payment.py
  - handlers/registration.py
  - handlers/states.py
  - main.py
  - services/scheduler.py
  - services/sheets.py
findings:
  critical: 2
  warning: 6
  info: 3
  total: 11
status: issues_found
---

# Phase 4: Code Review Report

**Reviewed:** 2026-06-30
**Depth:** standard
**Files Reviewed:** 8
**Status:** issues_found

## Summary

Phase 4 adds the consent, payment, and configurable reg-flow universal modules. The
architecture is sound: migrations are additive/idempotent, the atomic confirm guard in
`update_payment_status` is correct, the SHEET_HEADERS/`_build_sheet_row` column order is
aligned (40↔40), filter SQL uses a column whitelist with bound params, and scheduler job
targets take only picklable primitives.

Two correctness defects warrant blocking: (1) admin-entered payment requisites/penalties
are interpolated into a `parse_mode="HTML"` message **without escaping**, so a single `&`
or `<` in bank details silently breaks the payment-instruction send and leaves an approved
user with no instructions, no menu, and no FSM state; (2) the `arrival_date` reg-flow step
is collected but has no DB column, no `add_user` parameter, and no sheet column — its answer
is silently discarded. Six warnings cover a flow-restart fallback, a mass status mutation on
legacy users, consent never firing in short-form mode, and lost bonus/completion messaging.

## Critical Issues

### CR-01: Payment requisites & penalties not HTML-escaped — silent send failure strands approved users

**File:** `handlers/payment.py:106-120`
**Issue:** `_show_payment_details` builds an HTML message and sends it with `parse_mode="HTML"`.
`option_label` (line 103) and `deadline` (line 109) are passed through `html.escape`, but
`requisites` (line 107) and the penalty `date_part`/`amount` fields (line 115) are interpolated
raw. Admin-entered bank requisites and ФИО commonly contain `&` (e.g. "Карта Сбербанк & Тинькофф")
or `<`. Any such character makes Telegram reject the message with a parse error.

In the single/free path the raise is swallowed by `start_payment_step`'s try/except (payment.py:73),
and because the exception happens *before* `state.set_state(Registration.receipt_upload)` (line 121),
the user is left approved with **no payment instructions, no main menu, and no FSM state** — a dead end.
In the multi-option path (`process_payment_option`, no try/except) the callback handler simply errors.

**Fix:** Escape the same way the surrounding fields already are:
```python
if requisites:
    parts.append(f"📋 Реквизиты:\n{html.escape(requisites)}\n")
...
for line in penalties.strip().splitlines():
    if "|" in line:
        date_part, amount = line.split("|", 1)
        lines.append(f"• до {html.escape(date_part.strip())} — остаток {html.escape(amount.strip())} ₽")
```

### CR-02: `arrival_date` reg-flow step is collected but never persisted (silent data loss)

**File:** `handlers/registration.py:94` (and `database/db.py:124-135`, `database/db.py:181-297`)
**Issue:** `REG_FLOW` declares `("arrival_date", "reg_q_arrival_date", "date")`, the question has a
label/default/toggle, and `process_date_input` stores the answer into FSM data under key
`arrival_date` (registration.py:863-865). But there is **no `arrival_date` column** in the `users`
table (`db.py` migrates `arrival`, `birth_date`, etc. — never `arrival_date`), it is **not a parameter
in `add_user`** (db.py:181-297), and it is **not in `_build_sheet_row`/SHEET_HEADERS**. If an admin
enables this question, every answer is asked of the user and then silently discarded — it reaches
neither the DB nor the Google Sheet. (`birth_date`, the other `date`-type step, is wired correctly.)
**Fix:** Either remove the `arrival_date` row from `REG_FLOW` (the distinct `arrival` text field already
exists), or add the column and wire it end-to-end:
```python
# db.py init_db()
await _ensure_column(db, "users", "arrival_date", "TEXT")
# add_user INSERT column list + values + ON CONFLICT SET: arrival_date=excluded.arrival_date
# _build_sheet_row + SHEET_HEADERS: add the column in matching position
```

## Warnings

### WR-01: `_advance` ValueError fallback silently restarts the whole flow from step 0

**File:** `handlers/registration.py:460-470`
**Issue:** `_advance` looks up `enabled.index(after_step)`; on `ValueError` it sets `next_idx = 0`,
re-asking the first enabled step instead of finalizing or erroring. `enabled` is recomputed every
call from current FSM data, so if a question is toggled off (admin edits settings mid-registration)
or a conditional removes the just-answered step, `after_step` vanishes from the list and the user is
bounced back to question 1 — potentially repeatedly. A silent restart is the wrong failure mode.
**Fix:** On `ValueError`, advance to finalize (treat as end-of-flow) rather than restart:
```python
except ValueError:
    next_idx = len(enabled)  # fall through to summary/confirm instead of step 0
```

### WR-02: `sweep_payment_overdue` mass-flips ~590 legacy users to `overdue`

**File:** `services/scheduler.py:245-249`
**Issue:** The daily sweep runs `UPDATE users SET payment_status='overdue' WHERE payment_status='not_paid'`
once a `payment_deadline` passes. The Phase-4 migration defaults every pre-existing user (≈590) to
`payment_status='not_paid'`, and those users never entered the payment flow. The first time a deadline
is set and passes, all legacy `not_paid` rows are flipped to `overdue` — a bulk mutation of live data
unrelated to the current event's payers. It also makes them eligible for `send_payment_reminder`
(which only skips `paid`/`receipt_sent`/`None`, not `overdue`/`not_paid`).
**Fix:** Scope the sweep to users who actually have a payment journey, e.g. only those with a
`payment_option` set or `payment_due` populated:
```python
"UPDATE users SET payment_status='overdue' "
"WHERE payment_status='not_paid' AND payment_option IS NOT NULL"
```

### WR-03: Consent steps never collected in short-form registration

**File:** `handlers/registration.py:990-993` (with `_get_enabled_steps`:266-277)
**Issue:** Consent steps are appended only inside `_get_enabled_steps`, which is called only when
`registration_mode == "full"` (process_full_name:990 finalizes immediately for any other mode, and the
default is `short`). So enabling `consent_enabled` has **no effect** for an event using the short form —
required consents are silently skipped. For a consent-gated conference on the short form this defeats
the module entirely.
**Fix:** Run the enabled-steps engine (or at least the consent-append branch) even in short mode when
`consent_enabled == "on"`, or document/guard that consents require full-form mode (and surface that in
the admin UI when consent is toggled on while the form is short).

### WR-04: Payment-confirm path drops `reg_complete_text` and the registration bonus

**File:** `handlers/admin.py:2115-2125` (with `handlers/registration.py:1306-1321`)
**Issue:** When `payment_enabled == "on"`, `approve_user` returns right after `start_payment_step`
(registration.py:1306-1309), never sending `reg_complete_text` or the registration bonus. On receipt
confirmation, `rcpt_confirm` sends only "Оплата подтверждена" + main menu. The completion text and the
configured bonus (`reg_bonus_*`) are therefore never delivered for any paid event, even when bonus is
enabled. This is a silent feature regression for payment-enabled flows.
**Fix:** After a successful `rcpt_confirm`, deliver the same completion text + bonus block that the
non-payment path sends (factor that block out of `approve_user` into a reusable
`send_completion_and_bonus(bot, telegram_id)` and call it from both paths).

### WR-05: Single/free payment option still forces a receipt upload

**File:** `handlers/payment.py:59-72,96-121`
**Issue:** `paid = [o for o in options if o[1] > 0]` only gates the multi-option *selection menu*. The
single/free path (and the case of multiple options where none are paid) always calls
`_show_payment_details`, which unconditionally sets `Registration.receipt_upload` and asks the user to
"Загрузи чек оплаты" — even when the price is 0 (free participation) or `payment_options` is unset
(falls back to `("Участие", 0)`). A free participant is wrongly forced through a receipt-upload gate.
**Fix:** If the resolved `option_price == 0` (and no requisites), skip the receipt step — send the
completion/menu directly instead of entering `receipt_upload`.

### WR-06: `add_user` ON CONFLICT resets `payment_status` on re-registration

**File:** `database/db.py:238`
**Issue:** The upsert applies `payment_status=excluded.payment_status` (and `payment_due`, `paid_at`,
`payment_option`) unconditionally, where `excluded.payment_status` is `data.get('payment_status') or 'not_paid'`
(db.py:288). `receipt_file_id` and `resume_file_id` are protected with `COALESCE(...)`, but the other
payment columns are not. A rejected user who re-registers (the supported re-reg path, registration.py:793)
has their payment state wiped back to `not_paid`, discarding any prior `paid_at`/`payment_option`.
**Fix:** Guard the payment columns the same way, e.g. `payment_status=COALESCE(excluded.payment_status, users.payment_status)`
or omit them from the `DO UPDATE SET` clause so re-registration never touches payment state.

## Info

### IN-01: Registration progress denominator over-counts skipped steps

**File:** `handlers/registration.py:1002-1004` (with `_advance`:466-470)
**Issue:** `_reg_total` is fixed to `len(enabled)` computed before conditional answers. Conditional
skips (university/course/specialty when "не учусь", `work_sphere` when not working, `informal_day`
when Online) reduce the actual number of asked steps, so the user sees `(N/total)` that never reaches
`total`. Cosmetic only. **Fix:** Recompute/clamp the denominator after conditionals, or display only
the step index.

### IN-02: Resetting a paid user to `not_paid` leaves stale `paid_at`

**File:** `database/db.py:746-773`
**Issue:** `update_payment_status(uid, "not_paid")` (receipt rejection, admin.py:2152) does not clear
`paid_at`, so a previously-set `paid_at` lingers on a row that is no longer paid. No functional impact
today (no reader uses `paid_at` for a not_paid row) but it is misleading data. **Fix:** Clear
`paid_at`/`payment_due` when transitioning back to `not_paid`.

### IN-03: `settings_edit_value` indexes `data["setting_key"]` without guard

**File:** `handlers/admin.py:953`
**Issue:** `key = data["setting_key"]` will `KeyError` if the `EditSetting.waiting_for_value` state is
ever entered without `setting_key` populated (e.g. a stale state after a restart that somehow retains
the FSM). Low likelihood with MemoryStorage, but a `data.get("setting_key")` + early return is safer.
**Fix:** `key = data.get("setting_key"); if not key: await state.clear(); return`.

---

_Reviewed: 2026-06-30_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
