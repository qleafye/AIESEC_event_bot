---
phase: 04-universal-modules
reviewed: 2026-07-24T00:00:00Z
depth: deep
reviewer: adversarial-audit
files_reviewed: 8
files_reviewed_list:
  - handlers/payment.py
  - handlers/admin.py
  - handlers/registration.py
  - handlers/states.py
  - database/db.py
  - services/scheduler.py
  - services/sheets.py
  - main.py
findings:
  critical: 0
  high: 1
  medium: 2
  low: 4
  total: 7
status: issues_found
prior_review: 04-REVIEW.md (2026-06-30) — findings appear resolved (see below)
---

# Phase 4 «Universal Modules» — Adversarial Audit

**Reviewed:** 2026-07-24
**Depth:** deep (cross-file: payment ⇄ scheduler ⇄ admin ⇄ registration ⇄ db)
**Status:** issues_found

> A prior review exists at `04-REVIEW.md` (2026-06-30, 2 critical / 6 warning / 3 info).
> **It is preserved — this audit is a separate file.** All 2 criticals and 5 of 6 warnings
> from that review appear **resolved** in the current code (details in the last section).
> This audit is a *fresh* pass and surfaces defects the prior review did not cover.

## Overall Verdict

**SHIP-BLOCKED on one HIGH.** The payment/consent/toggle modules are well-built: the
`update_payment_status` confirm guard is genuinely atomic, migrations are additive/idempotent,
consent now runs before ФИО so it is enforced in both form modes, `arrival_date` is fully
wired end-to-end, and admin-entered requisites/penalties are HTML-escaped. The prior review's
blocking issues are gone.

However, the **receipt _reject_ path was never given the same atomic guard the _confirm_ path
has** — a confirm-then-reject sequence (trivially reachable with a single admin scrolling up to
a stale card, or two managers on the queue) silently resets a `paid` user back to `not_paid`
and lies to them that their receipt was rejected. That asymmetry is the one blocker. The
remaining findings are a wrong position counter at 1000+ scale, several `create_task` calls that
bypass the codebase's own GC-safe `_spawn` helper, and minor data-hygiene / validation gaps.

---

## HIGH

### H-01: Receipt **reject** path has no atomic guard — a confirm-then-reject race silently un-pays a paid user

**Files:** `handlers/admin.py:2821-2838` (`rcpt_reject_reason`) → `database/db.py:918-945` (`update_payment_status`)

`update_payment_status` guards **only** the `paid` transition:

```python
if status == "paid":
    where = "telegram_id = ? AND payment_status = 'receipt_sent'"   # atomic — good
else:
    where = "telegram_id = ?"                                        # UNCONDITIONAL
```

The reject handler calls the **unconditional** branch:

```python
# admin.py:2826
await update_payment_status(uid, "not_paid")   # resets ANY status, incl. 'paid'
```

**Attack / race trace:**
1. Manager A taps **✅ Подтвердить** on user X → `receipt_sent → paid` (rowcount 1), reminders cancelled, user told «Оплата подтверждена».
2. The card that A (or manager B) tapped is **not** edited/deleted after confirm — `rcpt_confirm` sends a *new* card via `_show_current_receipt_card` and leaves the old buttons live. Anyone scrolls up to X's old card and taps **❌ Отклонить**.
3. `rcpt_reject_reason` runs `update_payment_status(uid, "not_paid")` **unconditionally** → `paid → not_paid`.

**Impact:** a user who genuinely paid is flipped back to `not_paid`, is messaged «Чек отклонён… загрузи повторно», re-enters the «неоплатившие» broadcast segment, and — because `cancel_payment_reminders` already ran and is not re-scheduled — is now an un-tracked non-payer. Money-tracking data corruption from a one-tap footgun (worse with multiple `ADMIN_IDS`). The confirm path was hardened against exactly this; reject was not.

**Fix:** give reject the mirror guard so it only acts on a receipt still awaiting review.
Either make the handler tolerant of a 0-rowcount (stale card) *or* scope the SQL:

```python
# db.py update_payment_status — treat the reject reset like the confirm, not unconditional
if status == "paid":
    where = "telegram_id = ? AND payment_status = 'receipt_sent'"
elif status == "not_paid":
    where = "telegram_id = ? AND payment_status = 'receipt_sent'"   # don't clobber 'paid'
else:
    where = "telegram_id = ?"
```

```python
# admin.py rcpt_reject_reason — check rowcount, mirror rcpt_confirm's "already handled" branch
rows = await update_payment_status(uid, "not_paid")
if rows == 0:
    await message.answer("Чек уже обработан другим менеджером.")
    await state.set_state(None)
    await _show_current_receipt_card(message, state)
    return
```

---

## MEDIUM

### M-01: `create_task` calls bypass the codebase's own GC-safe `_spawn` helper — silent fire-and-forget loss

**Files:** `handlers/registration.py:2294, 2297`; `handlers/admin.py:1540, 2103, 2577, 2615, 2686, 2689`

`main.py:51-61` documents (WR-02) that the event loop keeps only a *weak* reference to a bare
`asyncio.create_task()`, so a suspended fire-and-forget task can be garbage-collected mid-run;
it introduces `_spawn()` (holds a strong ref in `_background_tasks`) precisely to prevent this.
Eight call sites bypass `_spawn` and call `asyncio.create_task(...)` directly. The two most
consequential are in `finalize_registration`:

```python
# registration.py:2294 / 2297 — the PRIMARY Sheets export for a completed registration
asyncio.create_task(append_to_party_sheet(_party_row))
asyncio.create_task(append_to_sheet(_sheet_row))
```

If either is collected before its `await` resumes, the registration **completes for the user but
its row never lands in the Google Sheet** — silent data loss on the exact path CLAUDE.md names as
the core value ("без ручного учёта в таблицах"). The admin.py sites (status-in-sheet updates,
album drain, welcome-flip drain) have the same latent hazard.

**Fix:** route all of these through the existing `_spawn` helper (export it, or add a small local
equivalent that stores the task in a module-level `set` with an `add_done_callback(discard)`), so
a strong ref is held until completion — identical to what `main.py` already does for startup tasks.

### M-02: Receipt-queue position counter is wrong for any queue >50 or with skips

**File:** `handlers/admin.py:2743-2744` (`_show_current_receipt_card`)

```python
current = visible[0]
position = total - len(visible) + 1
```

`visible` is a *single* batch (`limit=50`) minus skips, not the global index of `current`.
With `total=100` and no skips, the first (oldest) card shows **«Чек 51/100»** instead of 1/100,
because `len(visible)` caps at the 50-row batch. At the 1000–1500 scale CLAUDE.md mandates
pagination for, the counter is meaningless for the whole first page and jumps around as batches
roll. Purely a display defect (confirm/reject target the right `uid`), but user-facing and wrong.

**Fix:** track the true index. Simplest correct form — position = (already-processed) + 1:

```python
processed = total - (number still pending & not skipped)  # or pass offset through
# e.g. compute from a running counter kept in FSM state, or:
position = (offset - len(batch)) + (batch.index(current) + 1) - skipped_before_current
```
or drop the `/total` denominator and show only the remaining count.

---

## LOW

### L-01: Receipt document validation trusts the client-declared MIME type

**File:** `handlers/payment.py:365`

```python
if message.document.mime_type != "application/pdf":
```

`document.mime_type` is set by the *uploading client* and is spoofable — a non-PDF renamed/relabelled
`application/pdf` passes. Impact is low (the receipt is stored as a `file_id` and a human admin views
it in the tinder queue; no server-side parsing), so this is a UX guard, not a security boundary. Worth
a note only. If stricter filtering is desired, also check the filename extension, but manual admin
review is the real gate.

### L-02: Naive local time vs APScheduler timezone for deadlines/reminders

**Files:** `handlers/payment.py:166,174` (`_schedule_deadline_reminders`); `services/scheduler.py:258,261` (`sweep_payment_overdue`)

Deadlines are parsed with `datetime.strptime(...)` and compared to `datetime.now()` — both **naive
local**. `AsyncIOScheduler` (scheduler.py:84) is constructed with no explicit `timezone`, so it uses
`tzlocal`. In a container that runs UTC while the event is planned in MSK, T-3/T-1 reminders and the
overdue sweep fire at the wrong wall-clock time (and the `payment_plan_date` prompt shows a deadline
that doesn't match). Pin an explicit timezone on both the scheduler and the parse (e.g. store/compare
in the event's tz) to make wall-clock behaviour deployment-independent.

### L-03: Consent steps don't stamp `set_reg_step` — dropout analytics blind for consent abandons

**File:** `handlers/registration.py:739-763` (`_ask_step` consent branch) vs `1253` (`_ask_full_name` does stamp)

`_ask_full_name` records `set_reg_step(..., "full_name")` for dropout analytics, but the consent
branch of `_ask_step` never calls `set_reg_step("consent:<key>")`. Since consents now run *first*
(before ФИО), a user who bails on the very first consent card is recorded as «— (не начал отвечать)»
rather than «📋 Согласие», undercounting consent-stage dropout. Cosmetic/analytics only.

### L-04: Date-type steps validate format only — no range/sanity check

**File:** `handlers/registration.py:1586-1597` (`process_date_input`)

`datetime.strptime(raw, "%d.%m.%Y")` accepts any syntactically valid date, so `arrival_date` /
`payment_plan_date` accept dates in the past and `birth_date` accepts `01.01.1800` or a future year.
No functional break (stored as text, exported as-is), but the fields carry nonsense with no feedback.
Consider a light bound (e.g. plausible year range, or "not before today" for arrival/payment dates).

Also note **IN-02 from the prior review remains open**: the reject reset `update_payment_status(uid,
"not_paid")` (admin.py:2826) still does not clear `paid_at` / `payment_due`, so a rejected-after-paid
row keeps a stale `paid_at`. Once H-01 is fixed (reject no longer touches `paid` rows) this is
strictly cosmetic, but clearing `paid_at`/`payment_due` on the `not_paid` transition is the clean fix.

---

## Prior review (`04-REVIEW.md`) — resolution status

| Prior ID | Issue | Status in current code |
|----------|-------|------------------------|
| CR-01 | Requisites/penalties not HTML-escaped; state set after send | **Resolved** — `_format_requisites_block` (payment.py:67-74) + escaped penalties (payment.py:346); `set_state(receipt_upload)` now precedes the send (payment.py:354) with a fail-soft fallback (payment.py:227-238). |
| CR-02 | `arrival_date` collected but never persisted | **Resolved** — column (db.py:153), `add_user` param (db.py:340), `ON CONFLICT` set (db.py:280), sheet column (registration.py:996). |
| WR-01 | `_advance` ValueError restarts flow at step 0 | **Resolved** — `next_idx = len(enabled)` → finalize (registration.py:777). |
| WR-02 | Sweep mass-flips ~590 legacy `not_paid` | **Resolved** — scoped to `payment_option IS NOT NULL OR payment_due IS NOT NULL` (scheduler.py:263-265); defer path now persists `payment_due` (payment.py:171). |
| WR-03 | Consent never fires in short form | **Resolved** — consents run before ФИО for every mode (`_start_registration_flow`:1243-1248; short-form finalizes after, registration.py:1747). |
| WR-04 | Payment-confirm drops `reg_complete_text` + bonus | **Resolved** — `rcpt_confirm` calls `send_completion_and_bonus(...)` (admin.py:2795), factored into a shared helper (registration.py:2116). |
| WR-05 | Free/single option forces receipt upload | **Resolved** — free branch (`price==0 && no requisites`) sends completion + menu (payment.py:322-328); no-tariff track handled (payment.py:211-217). |
| WR-06 | `add_user` upsert resets payment state | **Resolved** — payment columns deliberately omitted from `DO UPDATE SET` (db.py:275-279). |
| IN-01 | Progress denominator over-counts | **Resolved** — `_advance` recomputes `total` fresh each step (registration.py:784). |
| IN-02 | Stale `paid_at` on reset to `not_paid` | **Open (minor)** — see L-04 tail; harmless once H-01 lands. |
| IN-03 | `settings_edit_value` unguarded `data["setting_key"]` | Not re-verified this pass; low-likelihood with MemoryStorage as noted originally. |

---

_Audited: 2026-07-24 — adversarial pass, depth=deep. Preserves 04-REVIEW.md._
