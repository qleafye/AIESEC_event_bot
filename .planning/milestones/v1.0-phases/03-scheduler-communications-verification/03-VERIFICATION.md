---
phase: 03-scheduler-communications-verification
verified: 2026-07-24T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: none
  note: "Initial verification. Code built via quick-tasks; no SUMMARY.md (expected). Code is source of truth."
---

# Phase 3: Scheduler + Communications + Verification — Verification Report

**Phase Goal:** Admins can schedule filtered broadcasts that survive bot restarts; incomplete registrations are auto-reminded; pre-selected users are gated at /start.
**Verified:** 2026-07-24
**Status:** PASSED (5/5)
**Re-verification:** No — initial verification against actual code (no SUMMARY.md; not treated as failure per instruction).

## Overall Verdict

**PASS — 5/5 success criteria verified against real code.** All scheduler, broadcast-filter, dropout-nudge, 429-hardening, and pre-selection-gate machinery exists, is substantive, is wired into `main.py` startup and the `/start` + `/broadcast` handlers, and data flows through real DB queries. 21/21 phase-3 unit tests pass.

## Goal Achievement — Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Scheduled broadcast survives restart, fires at correct time | PASS | `services/scheduler.py:84-91` (`SQLAlchemyJobStore` on `data/jobs.sqlite`), `:188-193` date job, payload in `scheduled_broadcasts` table `database/db.py:132-143` |
| 2 | Filtered broadcast with count preview, only matches receive | PASS | `handlers/admin.py:1918-1936` count preview; `database/db.py:817-869` parameterized AND filter; `admin.py:1939-1963` send-now/schedule |
| 3 | Abandoned registration gets exactly one nudge; registered never nudged | PASS | `services/scheduler.py:323-342`; `database/db.py:783-802` (`nudged_at IS NULL` + `mark_nudged`); `registration.py:2262` clears row on finalize |
| 4 | Broadcast loop waits retry_after+1 on 429, retry-success not counted blocked | PASS | `handlers/admin.py:1385-1395` (`_retry_delay`, `_classify_outcome`), applied at `:1550-1564` and `:1491-1508`; scheduler `_safe_send` `scheduler.py:130-146` |
| 5 | Pre-selection: not-in-sheet sees "отбор не пройден"+link; usernameless prompted | PASS | `handlers/registration.py:1330-1355`; `services/allowlist.py:39-59` |

**Score:** 5/5 truths verified

## Per-Criterion Detail

### Criterion 1 — Scheduled broadcast survives restart — PASS
- Scheduler built with persistent `SQLAlchemyJobStore(url="sqlite:///data/jobs.sqlite")` on a separate file, not forum.db (`scheduler.py:26,84-91`). `misfire_grace_time=86400` so a job whose run_date passed during downtime still fires on boot (`scheduler.py:86-91`).
- Date job registered via `schedule_broadcast_job(bid, when)` → `add_job(send_scheduled_broadcast, "date", args=[broadcast_id], id="bcast_{id}")` (`scheduler.py:188-193`; called from `admin.py:1638`). Job arg is an int id only — picklable, Bot from module global (`scheduler.py:149-186`) — so it restores cleanly across restart.
- Restart survival is the SQLAlchemyJobStore auto-load on `_scheduler.start()` (`scheduler.py:120`); `init_scheduler(bot)` is awaited at `main.py:136`. Payload persisted separately in `scheduled_broadcasts` (`db.py:132-143`, `create_scheduled_broadcast` `db.py:726-743`), read back by `get_scheduled_broadcast` on fire (`db.py:746-753`), status flipped to 'sent' (`mark_broadcast_sent` `db.py:756-761`), preventing double-send.
- Graceful shutdown closes the jobstore engine (`main.py:147-151`).

### Criterion 2 — Filtered broadcast with count preview — PASS
- Filter builder covers city/university/status/source/payment_status/participant_type + more, plus `registration_date` after/before threshold (`db.py:808-836`, `admin.py:1690-1708`). Multi-field specs are joined with `AND` (`db.py:835`). Values bind as `?`, columns are whitelisted (SQL-injection-safe).
- Count preview: `filter_count` handler renders "Под фильтр попадает **N** пользователей" from `len(count_and_list_filtered(filters))` BEFORE any send (`admin.py:1918-1936`, `db.py:862-869`).
- Only matches receive: same `count_and_list_filtered` id list feeds `filter_send_now` (`admin.py:1939-1950`) and, for scheduled, `filter_spec` JSON re-resolved at fire time (`scheduler.py:161-169`). Value picker pulls DB-distinct real values, no free-text (`admin.py:1808-1816`, `db.py:839-855`).

### Criterion 3 — Exactly one dropout nudge — PASS
- Interval scan job `nudge_incomplete_registrations` registered on the shared scheduler (`scheduler.py:94-98`), default 15 min, gated by `nudge_enabled` setting (default on, `scheduler.py:63-65,328`).
- Candidate query selects only `started_at < cutoff AND nudged_at IS NULL` (`db.py:783-791`); cutoff is now − `nudge_after_minutes` (default 120 = ~2h, `scheduler.py:330-331`).
- One-shot guarantee: `mark_nudged` stamps `nudged_at` only after a successful send (`scheduler.py:337-339`, `db.py:794-802`) — a second scan skips the row.
- Registered users never nudged: `clear_reg_started` deletes the row on finalize (`registration.py:2262`, `db.py:580-582`); legacy users never had a row.

### Criterion 4 — 429 handling — PASS
- `_retry_delay(retry_after) = retry_after + 1` (`admin.py:1385-1387`). On `TelegramRetryAfter` the loop sleeps that long then retries once (`admin.py:1555-1560`).
- `_classify_outcome`: first-try OR retry success ⇒ delivered (1,0); only genuine failure ⇒ blocked (0,1) — a rate-limited-then-delivered user is NOT counted blocked (`admin.py:1390-1395,1506-1508,1563+`). Same pattern in album send (`admin.py:1496-1508`) and scheduler `_safe_send` (`scheduler.py:136-143`).

### Criterion 5 — Pre-selection gate — PASS
- Gate at `/start`, default off, fail-soft (`registration.py:1328-1355`). Empty allowlist ⇒ fail-open admit with admin alert (`registration.py:1333-1336`, alert job `scheduler.py:347-364`).
- Usernameless user (and not in manual-id CSV) ⇒ prompt to set @username, returns (`registration.py:1340-1345`).
- Username not in sheet (and not manual-id) ⇒ "Отбор не пройден." + external link, returns (`registration.py:1346-1353`).
- Allowlist is a RAM set from a separate Sheet tab, normalized (strip/@/lower), refreshed at startup (`main.py:137`), on interval (`scheduler.py:100-104`), and on admin trigger (`services/allowlist.py:20-59`). Manual-id fallback via `_parse_manual_ids` CSV (`allowlist.py:25-36`).

## Key Link Verification

| From | To | Via | Status |
|------|----|----|--------|
| `main.py:136` | `init_scheduler` | `await` at startup | WIRED |
| `main.py:137` | `refresh_allowlist` | `_spawn` at startup | WIRED |
| `admin.py:1638` | `schedule_broadcast_job` | date job add | WIRED |
| `admin.py:1925/1946` | `count_and_list_filtered` | count preview + send | WIRED |
| `scheduler.py:95` | `nudge_incomplete_registrations` | interval job | WIRED |
| `registration.py:1332` | `services.allowlist.is_allowed` | import + call | WIRED |

## Data-Flow Trace (Level 4)

| Artifact | Data source | Real data | Status |
|----------|-------------|-----------|--------|
| `count_and_list_filtered` | `SELECT telegram_id FROM users{where}` | Yes (live users table) | FLOWING |
| `send_scheduled_broadcast` | `get_scheduled_broadcast(id)` + filter re-resolve | Yes | FLOWING |
| `get_nudge_candidates` | `reg_started` rows, nudged_at NULL | Yes | FLOWING |
| allowlist `is_allowed` | Google Sheet tab → RAM set via `refresh_allowlist` | Yes (fail-soft) | FLOWING |

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Phase-3 unit suite | `pytest tests/test_{allowlist,broadcast_429,filters,nudge,scheduler_helpers}_phase3.py` | 21 passed in 4.25s | PASS |

## Anti-Patterns Found

None blocking. No unreferenced TBD/FIXME/XXX markers in the phase files scanned. Note: `_is_allowed_resume` extension-only validation is a documented Phase-1 LOW risk (not in scope here).

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| SCHED-01 (persistent scheduler + scheduled broadcast) | SATISFIED | Criterion 1 |
| SCHED-03 (dropout nudge) | SATISFIED | Criterion 3 |
| COMM-01/02/03 (filter builder + count preview + send/schedule) | SATISFIED | Criterion 2 |
| COMM-04 (429 flood-safe) | SATISFIED | Criterion 4 |
| VERIF-01/02 (pre-selection gate + usernameless/manual-id) | SATISFIED | Criterion 5 |

## Human Verification Required

None mandatory for goal sign-off. Optional live smoke test if desired: schedule a broadcast 2 min out, restart the container, confirm it still fires (exercises jobstore auto-restore end-to-end against a real Telegram send — the only path not covered by unit tests).

## Gaps Summary

No gaps. All five ROADMAP success criteria are implemented, substantive, wired into startup/handlers, backed by real DB queries, and covered by passing tests. Phase goal achieved.

---

_Verified: 2026-07-24_
_Verifier: Claude (gsd-verifier)_
