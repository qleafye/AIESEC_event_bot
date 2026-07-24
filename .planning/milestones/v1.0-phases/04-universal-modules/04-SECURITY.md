# Phase 4 — Universal Modules: Security Audit

**Scope:** payment flow, receipt upload/verification, consent module, event-type + module toggles, payment-deadline scheduler.
**Files audited:** `handlers/payment.py`, `handlers/admin.py`, `handlers/registration.py`, `handlers/user_actions.py`, `database/db.py`, `services/scheduler.py`, `keyboards/builders.py`, `main.py`, `config.py`.
**Method:** code is source of truth. Every declared mitigation traced to the actual call site.

---

## Verdict: PASS (ship-able)

No CRITICAL or HIGH findings. The four highest-value attack surfaces — self-forging `payment_status=paid`, non-admin access to confirm/reject/toggles, double-confirm race, and SQL injection — are all correctly mitigated in code. Remaining findings are LOW (spoofable client MIME, no upload rate/size cap, admin-config-only amount parsing, FSM-only consent completeness). No blocker.

**Counts:** CRITICAL 0 · HIGH 0 · MEDIUM 0 · LOW 4 · Verified-mitigated 10

---

## Findings

### LOW-1 — Receipt document MIME check trusts client-declared `mime_type`
`handlers/payment.py:365`
```python
if message.document.mime_type != "application/pdf":
```
`message.document.mime_type` is the value Telegram relays from the uploader — it is not server-verified against file content and is spoofable (a renamed binary can carry `application/pdf`). Impact is bounded: the bot **never downloads or parses** the file — it stores only the opaque `file_id` (`_finalize_receipt` → `update_payment_status(..., receipt_file_id=file_id)`) and later re-sends it to an admin via `answer_document`/`answer_photo` (`admin.py:2860`). No server-side execution, no filename use, no path handling. Residual risk is social-engineering of the reviewing admin (a malicious file surfacing in the «🧾 Чеки» queue), where the admin's own client shows the true filename/type on download.
**Fix (optional/defense-in-depth):** keep the check as a UX filter; document in the manager guide that receipt files are untrusted user uploads and should be opened with normal caution. No code change required for the bot's own safety.

### LOW-2 — No size/rate limit on receipt or consent uploads
`handlers/payment.py:363-375`
Both `process_receipt_document` and `process_receipt_photo` accept uploads with no per-user throttle and no size gate. Because the bot only stores `file_id` (no download, no disk I/O), the bot-side DoS surface is minimal; the practical concern is queue-spam of the receipt tinder view. Telegram's own limits bound file size.
**Fix (optional):** add a lightweight per-user cooldown or ignore repeat uploads once `payment_status='receipt_sent'` (currently a re-upload just overwrites `receipt_file_id`, which is acceptable).

### LOW-3 — Payment amount parsing accepts negative values
`handlers/payment.py:98-133` (`_parse_options`)
`int(parts[1])` accepts negatives (e.g. `Билет|-5000`). Python ints don't overflow, and the value is **admin-config only** (from `payment_options` setting, set through the admin-gated settings editor) — not user input — and there is no payment gateway consuming it (payment is manual receipt + admin confirm). A negative price is treated as free (`paid = [v for v in visible if v[2] > 0]`), so no money-movement impact. Purely a display/config-hygiene issue.
**Fix (optional):** clamp `price = max(0, int(...))` or warn the admin on save.

### LOW-4 — Consent completeness enforced only by FSM ordering, not verified at finalize
`handlers/registration.py:1242-1246`, `1685-1716`; `database/db.py:874-892`
Required consents run as ordered steps before ФИО; `process_consent_accept` is gated on `Registration.consent_pending` **and** `_consent_key_matches` (stale/out-of-order taps rejected), and `process_consent_ignore` blocks advancing by text. This robustly prevents *skipping* a consent within a single registration run. However, `finalize_registration` does **not** re-verify that `get_user_consents(user_id)` covers every currently-required `consent_key`. Consequence is a compliance-completeness gap, not a bypass: a user who registered *before* an admin adds a new consent to `consent_list` keeps no record for it and is never re-prompted. Not exploitable by a user to finalize while skipping a live-required consent (the state machine forbids that).
**Fix (optional):** on finalize, log/flag users missing any required consent key; or re-prompt on next `/start` if `get_user_consents` is incomplete.

---

## Verified Mitigations (CLOSED)

| # | Threat | Evidence |
|---|--------|----------|
| 1 | User self-forges `payment_status='paid'` without a real receipt | Users can only reach `receipt_sent` (`_finalize_receipt`, `payment.py:391`) or `not_paid`. `'paid'` is written **only** by admin `rcpt_confirm` (`admin.py:2768`), and `update_payment_status` gates it with `WHERE telegram_id=? AND payment_status='receipt_sent'` (`db.py:935-936`). No user-reachable path sets `paid`. |
| 2 | Non-admin confirms/rejects/views a receipt | Every receipt callback re-checks `callback.from_user.id not in config.ADMIN_IDS`: `rcpt_confirm` (2764), `rcpt_reject_start` (2804), `rcpt_reject_reason` (`is_admin` msg filter, 2821), `rcpt_skip` (2843), `rcpt_view` (2862), `admin_receipts` (2754). |
| 3 | Double-confirm race (two managers approve same receipt) | Atomic conditional UPDATE returns `rowcount`; second concurrent confirm matches 0 rows and `rcpt_confirm` branches on `rows == 0` → "Чек уже обработан" with no duplicate side-effects (`admin.py:2768-2773`, `db.py:918-945`). |
| 4 | Non-admin flips `payment_enabled`/`consent_enabled` or changes `event_type` | `_toggle_module_setting` (`admin.py:624`) and `_toggle_value_setting` (669) both check `ADMIN_IDS`; `event_type` is written via `EditSetting.waiting_for_value` which carries the `is_admin` message filter (1104) before calling `_apply_event_type_preset` (1120). |
| 5 | SQL injection in payment/consent DB layer | All queries parameterized. `update_payment_status` builds its SET clause from a **hard-coded column whitelist** `("receipt_file_id","paid_at","payment_option","payment_due")` (`db.py:931`); `record_user_consent`/`get_user_consents` use bound params (`db.py:879-891`); `sweep_payment_overdue`'s f-string interpolates only a **constant** `select_where` string, no user data (`scheduler.py:263-274`). |
| 6 | Consent bypass via crafted callback / state manipulation | `consent_accept` requires `Registration.consent_pending` state **and** `_consent_key_matches(tapped, active)` (`registration.py:1685-1694`); text input can't advance (`process_consent_ignore`, 1712). Users cannot set their own FSM state; the only route to `full_name` is completing the ordered consent queue (`_ask_full_name`, 1709). |
| 7 | Consent record bound to wrong user | `record_user_consent(callback.from_user.id, consent_key)` (`registration.py:1695`) — keyed to the tapping user's own id. |
| 8 | Payment requisites leaked to pending/rejected users | The «💳 Оплата» entry calls `ensure_registered` (`user_actions.py:100`), which gates on approval `status` and blocks pending/rejected. The menu button itself renders only via `should_offer_receipt_upload` (`builders.py:31`), and the primary requisites disclosure runs inside `start_payment_step`, reached only from `approve_user` (post-approval) or the gated menu button. |
| 9 | Scheduler payload unsafe / reminders triggerable by non-admin | Date-job args are int-only (`args=[user_id]`, `scheduler.py:212`; `args=[broadcast_id]`, 192) — picklable primitives, no Bot/closure/untrusted-object pickling (Bot is a module global). `schedule_payment_reminder`/`cancel_payment_reminders` are internal functions with no user-facing callback; `send_payment_reminder` self-guards on `paid`/`receipt_sent` (`scheduler.py:235`). Broadcast `filter_spec` uses `json.loads`, not pickle (170-164). |
| 10 | Path/injection via `file_id` or filename | `file_id` is an opaque Telegram token stored verbatim and re-sent; no filesystem path is ever constructed and the uploaded filename is never read or used. |

---

## Notes / posture

- **Fail-safe defaults hold:** `payment_enabled`/`consent_enabled` default `off` (`admin.py:424-425`, `registration.py` presets); when off, the Phase-4 handlers never fire.
- **Callback authorization is defense-in-depth throughout** — message-level `is_admin` filters are consistently backstopped by per-callback `ADMIN_IDS` re-checks (the "D-06" pattern), which is the correct posture since inline callbacks aren't covered by message filters.
- **Reject path is safe:** `rcpt_reject_reason` resets to `not_paid` (re-upload allowed) and HTML-escapes the manager's reason before sending (`admin.py:2830`).
- **No payment gateway / webhook exists** — payment is manual receipt + admin confirm, so amount-tampering has no money-movement path.
