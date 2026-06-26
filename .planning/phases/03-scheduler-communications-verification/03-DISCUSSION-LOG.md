# Phase 3: Scheduler + Communications + Verification - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-27
**Phase:** 3-scheduler-communications-verification
**Areas discussed:** Scheduler backend, Pre-selection allowlist, Filtered broadcast UX, Dropout nudge

---

## Scheduler backend (SCHED-01)

| Option | Description | Selected |
|--------|-------------|----------|
| APScheduler + payload table | APScheduler+SQLAlchemyJobStore owns timing/persistence/restore; thin `scheduled_broadcasts` table holds payload, job stores only broadcast_id | ✓ |
| Custom table + startup loop only | No APScheduler; poll due rows — violates CLAUDE.md "no custom job table" | |
| Pure APScheduler (args pickled) | Pickle msg/filter into job args; opaque, hard to list/cancel in admin UI | |

**User's choice:** APScheduler + payload table (Recommended)
**Notes:** Resolves the CLAUDE.md (mandates APScheduler/SQLAlchemyJobStore) vs ROADMAP (says scheduled_broadcasts table) conflict — not mutually exclusive.

---

## Pre-selection allowlist (VERIF-01/02)

| Option | Description | Selected |
|--------|-------------|----------|
| Separate tab + cached | Dedicated worksheet/tab, username column; sheet1 unchanged; normalized match; in-memory cache + refresh; VERIF-02 prompt + manual telegram_id allowlist | ✓ |
| Reuse sheet1 username column | Mixes pre-selection allowlist with registered-users data — wrong (selection precedes registration) | |
| DB allowlist, sheet sync | Mirror sheet to local table; fastest lookups but adds sync step + non-instant edits | |

**User's choice:** Separate tab + cached (Recommended)
**Notes:** Selection happens before registration so sheet1 wouldn't contain selected users yet. Cache avoids gspread call on every /start.

---

## Filtered broadcast UX (COMM-01..03)

| Option | Description | Selected |
|--------|-------------|----------|
| Inline AND-builder + count | New "🎯 По фильтру" entry; field→value steps, AND-combine, count preview before send; reuses Broadcast FSM + process_broadcast | ✓ |
| Preset segments only | Fixed buttons; can't express arbitrary AND combos or date ranges — fails COMM-02/03 | |

**User's choice:** Inline AND-builder + count (Recommended)
**Notes:** Extends existing broadcast menu rather than a parallel system.

---

## Dropout nudge mechanism (SCHED-03)

| Option | Description | Selected |
|--------|-------------|----------|
| APScheduler interval job | Interval scan of reg_started (started_at>2h, nudged_at IS NULL) → one nudge → set nudged_at; one scheduling mechanism | ✓ |
| Standalone asyncio loop | Copy reminders.py pattern; second scheduling mechanism to maintain | |

**User's choice:** APScheduler interval job (Recommended)
**Notes:** Reuses the SCHED-01 scheduler. `reg_in_progress` in ROADMAP criteria == existing Phase-1 `reg_started` table; add `nudged_at` column, no new table.

---

## Claude's Discretion

- Cache refresh interval, 2h inactivity threshold, ~15min nudge-scan interval — sane defaults exposed as `bot_settings` keys.
- Filter-spec serialization (JSON blob vs columns) in `scheduled_broadcasts`.
- Manual telegram_id allowlist as setting string vs tiny table.

## Deferred Ideas

- Payment-deadline reminders T-3/T-1 — Phase 4 (PAY-06), reuses this scheduler.
- Consent module + event-type toggles — Phase 4.
- Recurring/cron broadcasts — not requested; one-time `date` triggers only.
