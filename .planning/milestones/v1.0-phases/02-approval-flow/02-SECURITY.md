# Phase 2 — Approval Flow — Security Audit

**Audited:** 2026-07-24
**Scope:** moderation UI ("Заявки"/"Чеки"), approve/reject/skip/approve-all callbacks, reject-reason FSM, atomic status flips, `ensure_registered` gating.
**Files reviewed:** `database/db.py`, `handlers/admin.py`, `handlers/registration.py`, `handlers/user_actions.py`, `main.py`.

## Overall Verdict

**PASS with one MEDIUM finding.** The security-critical properties of the approval flow are correctly implemented in code:
authorization is enforced on **every** approve/reject/skip/approve-all/receipt entry point, status flips are atomic with rowcount guards, callback IDs are int-parsed and used only in parameterized SQL, all free text is HTML-escaped, and bulk approve is rate-limited with 429 handling. No path lets a non-admin approve applications or self-approve.

The one substantive finding is a **fail-open ordering issue**: new users are inserted with a column default of `status='approved'` and only later corrected to `pending` by a best-effort, error-swallowing update — a crash or DB error in that window silently bypasses manual moderation.

**Findings by severity:** CRITICAL 0 · HIGH 0 · MEDIUM 1 · LOW 2

---

## Findings

### MEDIUM-1 — Fail-open moderation window (default `status='approved'`)
**Where:** `database/db.py:76` (`status TEXT DEFAULT 'approved'`), `handlers/registration.py:2254` (`add_user`) then `2279-2282` (`set_user_status` in a swallowed `try/except`).

**Issue:** `add_user()` does not set `status` in its INSERT (confirmed — column absent from the INSERT list, `db.py:214-231`), so a brand-new row lands as `approved` via the column default. The real status is computed *after* the insert (`_decide_status`, `registration.py:2274`) and written by `set_user_status()`, which is wrapped in `try/except` that only logs on failure (`2281-2282`). If that UPDATE fails, or the process crashes/restarts between `add_user` (2254) and `set_user_status` (2280), a user who should be `pending` under manual moderation remains `approved` and immediately clears the `ensure_registered` gate — a silent moderation bypass.

**Exploitability:** Low — not attacker-triggerable; requires an environmental fault (DB error / crash) in a narrow window. **Impact:** moderation bypass. The `approved` default is intentional for the ~590 brownfield users, but it makes the failure mode fail-**open**.

**Fix (concrete):** `_decide_status` needs no DB row (it reads only settings + `participant_type`), so compute `status` *before* `add_user` and pass it through the INSERT column list, making the persisted status atomic with row creation. Alternatively, change the column default to `'pending'` and set existing rows to `'approved'` once in the migration. Either removes the fail-open window entirely.

### LOW-1 — Rejection is not permanent (re-registration resets status)
**Where:** `handlers/registration.py:1370` (rejected user falls through to re-register, D-05a) + `add_user` ON CONFLICT DO UPDATE deliberately preserves `status` (`db.py:210-212`), but `set_user_status` (2280) then overwrites it with a freshly decided value.

**Issue:** A `rejected` applicant can send `/start` again, re-submit, and `set_user_status` will move them back to `pending` (or `approved` under auto mode). Rejection is therefore a soft state, not a ban. This appears intended (D-05a explicitly lets rejected users re-register), but it means a rejection can be trivially self-reverted. **Action:** document as accepted risk if intended; otherwise gate re-registration for `rejected` users.

### LOW-2 — Info-submenu callbacks not behind `ensure_registered`
**Where:** `handlers/user_actions.py` info callbacks (`info_date` ~134, `info_place` ~147, etc.).

**Issue:** These `callback_query` handlers do not call `ensure_registered`, so a `pending`/`rejected`/unregistered user replaying a crafted callback could view them. They expose only public event info (date, place, socials) — no personal or moderation data. Informational; recommend adding the gate for consistency.

---

## Verified Mitigations (threat-by-threat)

| Threat | Verdict | Evidence |
|--------|---------|----------|
| Non-admin approves/rejects/skips via crafted callback_data | **CLOSED** | Inline `callback.from_user.id not in config.ADMIN_IDS` guard on every handler: `admin.py:2517, 2527, 2546, 2569, 2591, 2635, 2653, 2678` (applications) and `2754, 2764, 2804, 2843, 2862` (receipts). No router-level bypass; `main.py:126` loads admin router but each handler self-checks. |
| Non-admin drives reject-reason FSM | **CLOSED** | Reject-reason message handlers carry the `is_admin` filter: `admin.py:2601, 2608` (applications) and `2814, 2821` (receipts). The state is only ever set from an admin-gated callback (`2589-2598`), and FSM state is per-(chat,user). |
| Self-approval | **CLOSED** | No self-service approval path exists; status transitions happen only through admin-gated handlers → `approve_user_atomic`/`reject_user`/`approve_all_pending`. |
| Double-approval / TOCTOU race | **CLOSED** | Atomic guarded UPDATE: `UPDATE users SET status='approved' WHERE telegram_id=? AND status='pending'` + `rowcount==1` return (`db.py:666-675`); reject symmetric (`678-686`). Second click → handler shows "Уже обработано" (`admin.py:2580-2581`). Bulk uses one atomic `UPDATE ... WHERE status='pending' RETURNING telegram_id` (`db.py:710-721`). |
| callback_data ID injection / arbitrary-row action | **CLOSED** | `_parse_appr` (`admin.py:2414-2422`) and `_parse_rcpt` (`2697-2705`) `int()`-parse with `ValueError→None`; IDs flow only into parameterized SQL. A crafted arbitrary int is harmless: only `pending` rows flip, and the whole path is admin-only anyway. |
| Bulk approve-all flood / 429 | **CLOSED** | `_welcome_flipped` (`admin.py:2660-2673`) drains sends with `asyncio.sleep(0.05)` between each and handles `TelegramRetryAfter` with backoff + single retry; sends run in a background task so the DB flip is not blocked. |
| Reject-reason HTML/markdown injection | **CLOSED** | Reason and admin-set prefix both `html_module.escape`-d before send: `admin.py:2622` (applications), `2830` (receipts). Reason never reaches SQL — `reject_user` takes only `tid`. |
| Reject-reason SQL injection | **CLOSED** | Free text is only logged and echoed to the user; no query interpolation. |
| Notification cross-user data leak | **CLOSED** | Approve/reject/receipt notifications are addressed to the single affected `uid` only (`admin.py:2621, 2777, 2833`); no other applicant's data is included. |
| Pending/rejected user reaches gated actions | **CLOSED (with LOW-2 caveat)** | `ensure_registered` → `_gate_decision` denies `pending` and `rejected` (`user_actions.py:27-55`) and is called on every message-triggered menu action (`user_actions.py:76, 84, 100, 112, 181, 206, 227, 262, 275, 299`). Gap limited to public info-only callbacks (LOW-2). |
| Admin-notify content escaping (new pending) | **CLOSED** | Name/username/LC/position/source all `html.escape`-d in the pending-notify block (`registration.py:2306-2322`). |

---

## Summary

Core approval-flow threats (authorization, atomicity, injection, output escaping, flood) are all mitigated in the actual code. Ship-blocking status depends on treatment of **MEDIUM-1**: recommend making the persisted status atomic with row creation (fail-safe default) before high-volume moderation season. LOW-1 and LOW-2 are documentation / hardening items.
