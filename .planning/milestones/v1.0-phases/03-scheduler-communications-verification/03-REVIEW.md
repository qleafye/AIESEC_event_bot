---
phase: 03-scheduler-communications-verification
reviewed: 2026-07-24T00:00:00Z
depth: deep
files_reviewed: 6
files_reviewed_list:
  - main.py
  - services/scheduler.py
  - services/allowlist.py
  - services/reminders.py
  - handlers/admin.py
  - handlers/registration.py
  - database/db.py
  - services/sheets.py
findings:
  critical: 0
  high: 1
  medium: 5
  low: 6
  total: 12
status: issues_found
---

# Phase 3: Code Review Report — Scheduler + Communications + Verification

**Reviewed:** 2026-07-24
**Depth:** deep (cross-file)
**Files Reviewed:** main.py, services/scheduler.py, services/allowlist.py, services/reminders.py, handlers/admin.py (broadcast/schedule/filter), handlers/registration.py (gate), database/db.py, services/sheets.py

## Overall Verdict

**SHIP WITH FIXES.** The scheduler foundation is sound on the happy path: job ids are namespaced (`bcast_{id}`, `pay_reminder_{id}_{label}`), interval jobs use `replace_existing=True`, date jobs auto-restore, `send_scheduled_broadcast`/`send_payment_reminder` carry idempotency + fire-time re-gating guards, and every scheduled callback is wrapped in try/except. The 429 single-retry pattern is bounded (no infinite loop) and correctly classifies a retry-success as delivered.

However there is **one HIGH data-delivery bug** — the album broadcast is launched with a bare `asyncio.create_task` that bypasses the project's own `_spawn` GC-safety helper, so the entire album send can be silently garbage-collected mid-run (the exact hazard main.py:WR-02 was written to prevent). Plus several MEDIUM correctness/robustness gaps around timezone-naive scheduling, duplicate-on-crash broadcasts, orphaned "pending" rows past the misfire grace, an empty-filter fan-out to all users, and the pre-selection gate locking out already-registered users.

No CRITICAL security issues: filter columns are whitelisted and values bind as `?` (no SQL injection), gate normalization is consistent, CSV export is formula-injection-safe.

## High

### HI-01: Album broadcast task is GC-eligible mid-run (bypasses `_spawn`)

**File:** `handlers/admin.py:1540`
**Issue:** `asyncio.create_task(_wait_and_send_album(mgid, users_ids, bot, state, message.from_user.id))` is fire-and-forget with no strong reference retained. This is the identical hazard the codebase documents and fixes in `main.py:51-61` (WR-02 / `_spawn`): the event loop keeps only a weak reference, so the task — which spends most of its life suspended on `asyncio.sleep(0.8)` and per-recipient `asyncio.sleep(0.05)` — can be garbage-collected before completion. If collected, the entire album broadcast silently never sends, the admin gets no completion report, and the `pending_albums[mgid]` entry leaks forever (never popped). Every other Phase-3 send path correctly holds refs (`_safe_send` loops run inside the awaited job coroutine). This one does not.
**Fix:** Route through a retained-reference helper, mirroring `main._spawn`:
```python
# module-level in handlers/admin.py
_bg_tasks: set[asyncio.Task] = set()
def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t
# line 1540:
_spawn(_wait_and_send_album(mgid, users_ids, bot, state, message.from_user.id))
```
(Same fix applies to the other bare `create_task` calls flagged in LO-05.)

## Medium

### ME-01: Scheduler has no explicit timezone — scheduled times are wall-clock ambiguous

**File:** `services/scheduler.py:84-91` (also `handlers/admin.py:1600,1604,1637`)
**Issue:** `AsyncIOScheduler(...)` is constructed with no `timezone=`, so APScheduler falls back to the host's `tzlocal`. `_parse_schedule_dt` returns a **naive** datetime and `broadcast_schedule_when` compares it against a **naive** `datetime.now()`. On a single host this is internally consistent, but a container defaulting to UTC (the common case) will fire an admin-entered "14:30" broadcast at 14:30 UTC = 17:30 Moscow — 3 hours off from what a Russian DXP manager intends. There is no TZ pinned anywhere in the deployment (Dockerfile/compose set none). Payment reminders and the overdue sweep inherit the same drift.
**Fix:** Pin the timezone explicitly and keep comparisons tz-aware:
```python
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Europe/Moscow")
_scheduler = AsyncIOScheduler(timezone=TZ, jobstores=..., job_defaults=...)
# and localize parsed admin datetimes before add_job / now() comparison
```

### ME-02: Duplicate broadcast on mid-send crash

**File:** `services/scheduler.py:149-183`
**Issue:** `send_scheduled_broadcast` calls `mark_broadcast_sent(broadcast_id)` only AFTER the full per-recipient loop (which for 1000+ users + retry sleeps can run for minutes). If the process crashes/restarts mid-loop, the row stays `status='pending'`, and because `misfire_grace_time=86400` + `coalesce=True` the restored date job re-fires and re-sends to **every** recipient from the top (the status guard at line 158 doesn't help — status is still `pending`). There is no per-recipient delivery ledger, so already-messaged users are re-spammed.
**Fix:** Mark `sent` (or a `sending` intermediate) before the loop, or persist a per-recipient cursor / delivered-set so a re-fire resumes instead of restarting. At minimum flip status to a non-`pending` sentinel at loop start so a crash cannot re-broadcast to everyone.

### ME-03: Orphaned "pending" broadcasts after downtime > misfire grace

**File:** `services/scheduler.py:90,149`; `database/db.py:764-770`
**Issue:** The WR-04 comment sets `misfire_grace_time=86400` so a job whose `run_date` passed during ≤24h downtime still fires. But if downtime exceeds 24h, APScheduler drops the date job while `scheduled_broadcasts.status` remains `'pending'` forever. There is no startup reconciliation that scans for `status='pending' AND scheduled_at < now` to re-schedule, mark failed, or alert. `/scheduled` (`list_pending_broadcasts`) will keep listing a broadcast that can never fire, and an admin tapping nothing will assume it's still queued.
**Fix:** On `init_scheduler`, after `start()`, sweep `list_pending_broadcasts()`; for rows whose `scheduled_at` is in the past and have no live job id in the store, either re-`add_job` (immediate) or mark `status='missed'` and alert admins.

### ME-04: Empty / all-dropped filter fans out to ALL users instead of failing safe

**File:** `database/db.py:817-836`; consumed at `services/scheduler.py:161-169`, `handlers/admin.py:1925,1946`
**Issue:** `_build_filter_clause` silently skips any non-whitelisted field and, if that leaves zero clauses, returns `where=""`. `count_and_list_filtered` then runs `SELECT telegram_id FROM users` with no WHERE — i.e. the **entire** user base. For a *scheduled* filtered broadcast, `filter_spec` is JSON persisted at schedule time and re-evaluated at fire time (scheduler.py:164); if the whitelist changes (e.g. a column is removed) between schedule and fire, every condition is dropped and the message blasts all 1000+ users instead of the intended segment. A filter that the operator built should never degrade to "everyone."
**Fix:** Distinguish "no filter requested" from "filter requested but produced no clauses." Have `_build_filter_clause` (or its callers) return a sentinel/raise when the input list is non-empty but yields zero valid clauses, and refuse to send in `send_scheduled_broadcast`.

### ME-05: Pre-selection gate can lock out already-registered participants

**File:** `handlers/registration.py:1328-1355`
**Issue:** The gate runs BEFORE `get_user()` (line 1357), so it applies to *every* `/start`, including users who already completed and were approved. If an admin toggles `preselect_enabled` ON mid-event and a previously-registered delegate's `@username` isn't in the «Отобранные» tab (renamed, changed handle, or tab only lists newly-selected people), that delegate gets "Отбор не пройден." and can no longer reach their own menu / referral link / payment button. The gate should not re-gate people who are already in the system.
**Fix:** Fetch the user first and short-circuit the gate for already-registered non-rejected users:
```python
_existing = await get_user(user_id)
if _existing and (_existing.get("status") or "approved") != "rejected":
    pass  # skip pre-selection gate — already admitted
else:
    ... existing gate ...
```

## Low

### LO-01: Startup allowlist race — first `/start` fail-opens

**File:** `main.py:137`; `services/allowlist.py:48-59`
**Issue:** `refresh_allowlist()` is spawned (not awaited) at startup, and `_allowlist` begins empty. A `/start` arriving in the ~1s boot window before the Sheet load completes sees `allowlist_size()==0` and fail-opens (admits everyone). Consistent with the owner-confirmed fail-open posture, but worth an explicit note since it is a silent bypass window.
**Fix:** Optionally `await refresh_allowlist()` before `start_polling`, accepting a small startup delay, or gate `/start` on "refresh has completed at least once."

### LO-02: `sweep_payment_overdue` SELECT-then-UPDATE is not atomic

**File:** `services/scheduler.py:263-288`
**Issue:** `overdue_ids` is collected by SELECT, then a separate UPDATE flips rows. A user who uploads a receipt (status → `receipt_sent`) between the two statements is correctly skipped by the UPDATE's `payment_status='not_paid'` guard, but their id is already in `overdue_ids`, so they still receive an "срок оплаты истёк" ping despite now being in review.
**Fix:** Use `... RETURNING telegram_id` on the UPDATE (SQLite ≥3.35, already relied on in `approve_all_pending`) so the ping list is exactly the rows actually flipped.

### LO-03: Unbounded retry sleep can stall a whole broadcast/job

**File:** `services/scheduler.py:137`; `handlers/admin.py:1385-1387,1498,1557`
**Issue:** `asyncio.sleep(e.retry_after + 1)` honors whatever delay Telegram dictates with no ceiling. A large `retry_after` (flood-wait can be tens of seconds to minutes) blocks the single job coroutine / broadcast loop for the whole duration. Not an infinite loop, but one recipient's flood-wait pauses delivery to everyone after them.
**Fix:** Cap the wait (e.g. `min(e.retry_after, 60) + 1`) and/or log when the delay exceeds a threshold.

### LO-04: `registration_date` "после" (after) uses `>=`, includes the boundary day

**File:** `database/db.py:827-830`
**Issue:** For `op == "after"` the clause is `registration_date >= ?` with the value being a bare date (`date(registration_date)` from the picker). "После 01.07" therefore *includes* everyone who registered on 01.07, which contradicts a strict "after that date" reading; "до" (`<`) correctly excludes the day. Minor segmentation skew.
**Fix:** Decide inclusive/exclusive intent explicitly and document it in the picker label, or use `> date+1day` for a strict "after."

### LO-05: Other bare `create_task` calls share the GC hazard (pre-existing)

**File:** `handlers/admin.py:2103,2577,2615,2686,2689`
**Issue:** Same class as HI-01 — status-in-sheet updates, the welcome-flip drain, and header ensure are all fire-and-forget without a retained reference. Pre-existing (not new Phase-3 code) but they can be GC'd mid-run; the welcome-drain (2686) and status-sync tasks are the most user-visible if dropped.
**Fix:** Migrate all of these to the `_spawn` helper from HI-01.

### LO-06: `pending_albums` entry leaks if the album task never runs

**File:** `handlers/admin.py:1537-1543`
**Issue:** The dict entry is created on the first album message and only ever removed by `_wait_and_send_album`'s `pop`. If that task is GC'd (HI-01) or errors before the pop, the entry — holding `Message` objects — stays resident for the process lifetime. Compounds HI-01.
**Fix:** Fixing HI-01 removes the main leak path; additionally pop under a try/finally and consider a TTL sweep for stale media-group ids.

---

_Reviewed: 2026-07-24_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
