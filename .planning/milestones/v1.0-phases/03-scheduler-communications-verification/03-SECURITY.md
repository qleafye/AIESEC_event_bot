# Phase 3 Security Audit — Scheduler + Communications + Verification

**Audited:** 2026-07-24
**Scope:** `main.py`, `services/scheduler.py`, `services/allowlist.py`, `services/reminders.py`, `handlers/admin.py` (broadcast/schedule/filter surface), `handlers/registration.py` (pre-selection gate), `database/db.py` (filter builder + broadcast/nudge stores)
**Method:** Code is source of truth. Each declared mitigation verified by locating the actual enforcing call, not documentation.

---

## Verdict: PASS — no CRITICAL or HIGH findings

All seven focus-area threats are mitigated in code. The SQL filter builder is whitelisted and parameterized, every broadcast/schedule/filter entry point enforces an admin check, scheduler job payloads carry only picklable int ids (no pickle of untrusted data), and the 429 retry path is bounded to a single retry. Residual items are 2 MEDIUM (both by-design / accepted-risk properties of the pre-selection gate) and 3 LOW (reliability / defense-in-depth notes).

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 2 |
| LOW | 3 |

No HIGH/CRITICAL findings to enumerate.

---

## Threat verification (focus areas)

### 1. Filter-builder SQL injection — CLOSED
`database/db.py:807-869`.
- Column names are composed into SQL **only** from the `_FILTER_COLUMNS` whitelist (db.py:808-814) plus the single literal `registration_date`. `_build_filter_clause` (db.py:817-836) drops any non-whitelisted `field` silently — it is never f-stringed.
- Filter **values** are never interpolated; they bind as `?` (db.py:830, 833, 867).
- `get_distinct_filter_values` (db.py:839-859) re-validates `field` against the same whitelist before the only f-string column composition; unknown field → `return []`.
- Handler layer is doubly safe: field is chosen from `_PICKER_FIELDS`/`filter_f_*` callbacks (admin.py:1819-1826), value is a **bounds-checked index** into a DB-derived options list (admin.py:1884-1892), never free text.
**No user- or admin-typed string reaches a column position.**

### 2. Authorization on broadcast / schedule / filter / send-now — CLOSED
Every entry point re-checks `callback.from_user.id in config.ADMIN_IDS` (callbacks are not covered by the message-level `is_admin` filter — WR-01/D-06):
- `show_admin_broadcast` admin.py:1250; `broadcast_all` :1299; `broadcast_local` :1317; `_start_segment_broadcast` :1364 (covers unsubscribed :1400 and incomplete :1413 — double-guarded); `broadcast_cancel` :1427.
- Schedule: `broadcast_schedule_start` :1582; message states `schedule_when`/`schedule_message` carry `is_admin` filter (admin.py:1598, 1616); `cmd_scheduled` :1648 `is_admin`; `sched_cancel` :1669.
- Filter builder: `broadcast_filter_start` :1799; `filter_pick_field` :1822; `filter_pick_date` :1832; `filter_pick_date_op` :1846; `filter_page_nav` :1857; `filter_pick_value` :1878; `filter_back` :1909; `filter_count` :1920; `filter_send_now` :1941; `filter_schedule` :1955.
- `config.ADMIN_IDS` is a required `.env` field with no default (config.py:8) — no empty/all-admin fallback.
**A non-admin cannot trigger a broadcast, build a filter, or cancel a scheduled job.**

### 3. Scheduler job payload deserialization — CLOSED
`services/scheduler.py`.
- Date/interval job targets receive **only** picklable primitives: `send_scheduled_broadcast(broadcast_id: int)` (:149, scheduled with `args=[broadcast_id]` :191), `send_payment_reminder(user_id: int)` (:225), and no-arg interval jobs. The Bot is injected via a module global (:31, :80-81), never pickled into a job (Pitfall 3).
- The broadcast **payload** (`filter_spec`) is stored as JSON in the app DB and read back with `json.loads` (:164), not pickle — no `eval`, no `pickle.loads` of untrusted content anywhere in the phase.
- The SQLAlchemyJobStore pickle blob lives in a **separate local file** `data/jobs.sqlite` (:26) written only by the bot process; it is not reachable/writable through any bot-facing input path. Trust reduces to filesystem integrity of the deploy volume.

### 4. Broadcast content injection / parse-mode crash — CLOSED
- Default parse mode is HTML (main.py:102). Instant broadcasts use `message.send_copy` (admin.py:1553) / `send_media_group` (:1494), which copy the admin's own entities — no re-parse.
- Scheduled broadcasts send stored `html_text` via `_bot.send_message(cid, text)` (scheduler.py:179). Content is **admin-authored**, so this is not a privilege boundary; a malformed-HTML 400 is swallowed by `_safe_send` (scheduler.py:130-146) and never crashes the loop.
- `/scheduled` preview strips tags and `html.escape`s the result before display (admin.py:1657-1662) — no tag reflection.

### 5. Pre-selection Google-Sheet allowlist — CLOSED (with residual notes M1/M2)
`services/allowlist.py` + `handlers/registration.py:1328-1355`.
- Case/whitespace/`@` spoofing is neutralized: `_normalize` lowercases and strips both the sheet entries and the incoming username (allowlist.py:20-22, 41, 56). No case-sensitivity bypass.
- Manual-id fallback (`preselect_manual_ids`) is an **admin-only bot_setting** parsed by `_parse_manual_ids` (allowlist.py:25-36); a user cannot write it, so it is not a self-authorization vector.
- No-username users are blocked unless explicitly in `manual_ids` (registration.py:1340-1345); admin-controlled fail/prompt/link text is `html.escape`d before send (:1344, 1349-1351).
- Sheet data is admin-owned and read-only (`col_values(1)`, sheets.py:112).
See M1 (fail-open when the list is empty) and M2 (username reassignment) below — both known/by-design residuals, not code defects.

### 6. Rate-limit / 429 flood handling — CLOSED
- `_safe_send` (scheduler.py:130-146) retries **exactly once** after `TelegramRetryAfter`, then gives up — no unbounded loop.
- Admin broadcast path mirrors this: single retry via `_retry_delay` (admin.py:1385-1387) then `_classify_outcome` (:1390-1395). Inter-send throttle `asyncio.sleep(0.05)` on every loop (scheduler.py:180, 288, 340; admin.py:1509, 1568).

### 7. `reg_in_progress` dropout nudge — CLOSED
`services/scheduler.py:323-342`, `database/db.py:783-802`.
- One-shot dedup: `get_nudge_candidates` selects only rows with `nudged_at IS NULL` (db.py:788); `mark_nudged` stamps the row **only after a successful send** (scheduler.py:338-339). No repeat spam.
- Nudge text is an admin setting / static default (scheduler.py:295-298, 335); no user-supplied content is echoed, so no data-leak channel.
- Candidate query returns bare `telegram_id`s; the nudge message contains no per-user PII.

---

## Findings

### MEDIUM

**M1 — Pre-selection gate is fail-OPEN when the allowlist is empty (accepted risk).**
`handlers/registration.py:1333-1336`, `services/scheduler.py:353-362`.
When `preselect_enabled == "on"` but the RAM allowlist is empty (sheet unreachable / quota error / bad tab), the gate admits **every** user and only logs a warning + pings admins. This is an owner-confirmed decision (avoid locking a whole event over a Sheets glitch) and is loudly alarmed, but it is a security-relevant weakening: a transient Google outage silently disables the selection barrier.
*Fix (optional, if a stricter posture is ever wanted):* add a `preselect_fail_closed` toggle so an operator can opt into rejecting `/start` while the list is empty, defaulting to today's fail-open behavior.

**M2 — Username-based allowlist is bypassable via Telegram username reassignment (inherent).**
`services/allowlist.py:39-41`, `handlers/registration.py:1346`.
The gate trusts `message.from_user.username`. Telegram usernames are globally unique but **reassignable**: if a selected delegate releases/changes their `@username`, a different user can claim the freed handle and pass the gate. Normalization prevents case tricks but cannot prevent legitimate reassignment.
*Fix (optional):* for high-assurance events, gate on `telegram_id` (the `preselect_manual_ids` path already does this) rather than username, or capture the delegate's numeric id at selection time.

### LOW

**L1 — Album-broadcast task is fire-and-forget without a strong reference.**
`handlers/admin.py:1540` (`asyncio.create_task(_wait_and_send_album(...))`).
Unlike `main.py`'s `_spawn` helper (which holds strong refs per WR-02), this task is not retained; under load the event loop may GC it mid-broadcast, silently truncating an album send. Reliability, not a security boundary — but it is the same class of bug WR-02 fixes elsewhere.
*Fix:* route it through the same strong-ref set / a module-level task registry.

**L2 — SQLAlchemyJobStore pickle trust depends on filesystem integrity.**
`services/scheduler.py:26, 84-91`.
Job args are int-only (safe), but the jobstore persists pickled callables. If an attacker ever gains write access to `data/jobs.sqlite`, a crafted pickle would deserialize on next boot. Not reachable through any bot input today; mitigation is deploy-volume permissions.
*Fix:* ensure `data/` is not world-writable in the container/host; document it in SECURITY hardening notes.

**L3 — Admin-authored HTML broadcasts rely on Telegram-side rejection.**
`services/scheduler.py:179`, `handlers/admin.py:1521-1575`.
Broadcast text is sent with the global HTML parse mode. Since authors are admins this is not an injection boundary, and `_safe_send` swallows a malformed-HTML 400 — noted only for completeness. No action required.

---

## Mitigated threats confirmed present (summary table)

| Threat | Disposition | Evidence |
|--------|-------------|----------|
| SQL injection via filter field/value | mitigate | db.py:808-836 (whitelist + `?` binding), :849-853 re-validated |
| Non-admin broadcast/schedule/filter | mitigate | admin.py admin-check at 1250/1299/1317/1364/1400/1413/1427/1582/1669/1799-1955 |
| Unsafe scheduler payload (pickle/eval) | mitigate | scheduler.py:149/191/225 int-only args; :164 `json.loads`; :26 separate local jobstore |
| Broadcast content injection/crash | mitigate | admin.py:1553 send_copy; scheduler.py:130-146 `_safe_send`; admin.py:1657-1662 escape |
| Allowlist case/manual-id bypass | mitigate | allowlist.py:20-22/25-36; registration.py:1340-1352 |
| 429 infinite retry | mitigate | scheduler.py:130-146 single retry; admin.py:1385-1395 |
| Nudge spam / data leak | mitigate | db.py:788 `nudged_at IS NULL`; scheduler.py:338-339 stamp-after-success |
| Pre-selection fail-open (empty list) | accept (M1) | registration.py:1333-1336; alarmed at scheduler.py:353-362 |
| Username reassignment bypass | accept (M2) | inherent to username gate; id-based path available |
