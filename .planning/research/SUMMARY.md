# Project Research Summary

**Project:** AIESEC Event Bot — brownfield milestone (YL’26 + universal modules)
**Domain:** Telegram event-registration bot (aiogram 3 + aiosqlite + SQLite)
**Researched:** 2026-06-25
**Confidence:** HIGH

## Executive Summary

This is a brownfield extension of a production aiogram 3 bot serving 1000–1500 users per season. The existing architecture (dynamic REG_FLOW engine, bot_settings KV config, open-per-call aiosqlite pattern, gspread sync) is sound and must be extended, not replaced. The milestone adds an approval queue, coins economy, channel subscription gate, scheduled broadcasts, a payment module, and an event-modularity toggle system — all integrated into the existing handler/service structure through additive DB migrations and new router files.

The recommended approach is strictly incremental: start with DB schema migrations (safe to deploy immediately due to `DEFAULT 'approved'` on the new `status` field), add coins as a standalone service, then build the approval flow before anything that reads `status=approved`. APScheduler 3.x with `AsyncIOScheduler` is required for all scheduler features; whether to use `SQLAlchemyJobStore` or a manual MemoryJobStore+DB-restore pattern is the one open decision (see reconciliation section below). Everything else — getChatMember subscription checks, file_id storage, DB migrations — uses built-in aiogram 3 capabilities and the existing `_ensure_column` pattern, requiring no additional dependencies beyond APScheduler itself.

The dominant risks are carry-over bugs from the prior bot: non-atomic coins writes caused the production "слетали баллы" failure, and `INSERT OR REPLACE` on `users` will silently destroy the new `status` field on re-registration unless patched immediately. Both are straightforward to fix if addressed in the correct phase. Secondary risks are broadcast rate-limiting (current `sleep(0.05)` has no retry on 429) and concurrent manager double-approvals (require an atomic SQL UPDATE guard). All critical pitfalls have clear, low-effort mitigations.

---

## Key Findings

### Recommended Stack

The existing stack is frozen: aiogram 3.29.0, aiosqlite, pydantic-settings, gspread + google-auth, aiohttp-socks, long polling. No replacement or migration is needed. The only new production dependency is `apscheduler==3.11.2` (latest stable 3.x; 4.0.0a6 is still alpha with a breaking API). Whether `sqlalchemy>=2.0` is also added depends on the scheduler persistence decision below.

DB migrations continue using the existing `_ensure_column()` + `CREATE TABLE IF NOT EXISTS` pattern in `db.py`. `PRAGMA user_version` should be added for migration state tracking. Alembic and yoyo-migrations are explicitly ruled out — both require SQLAlchemy ORM or synchronous-only execution incompatible with the aiosqlite model.

**Core technologies:**
- `aiogram 3.29.0`: async Telegram framework — frozen, no change
- `aiosqlite`: async SQLite driver — frozen; open-per-call pattern retained
- `apscheduler==3.11.2`: persistent scheduled jobs for broadcasts and reminders — only new mandatory dependency
- `getChatMember` (aiogram built-in): channel subscription gate — no new library needed
- `file_id` storage (TEXT column): receipt and resume storage — no disk I/O, no new library
- `PRAGMA user_version`: schema version tracking — SQLite built-in, no new library

### Expected Features

**Must have (table stakes — P1 and core milestone):**
- Approval flow: `status` field (pending/approved/rejected) + split `finalize_registration()` — central milestone requirement
- Tinder UI (paginated card review): application queue usable at 1000+ scale; pattern reused for receipt review
- Coins transactions log (INSERT-only audit trail): prevents the known data-loss bug; never a `balance` column
- Channel subscription gate: standard gate, LOW complexity, `getChatMember` built-in
- Consent module: legal requirement, injected into REG_FLOW before finalization
- APScheduler persistent infrastructure: cross-cutting enabler for all reminder features
- Moderation toggles per form variant (`short_approval`/`full_approval`): needed before rollout

**Should have (differentiators — P2):**
- Leaderboard (top-10 + own rank): completes coins feature once table exists
- Pre-selected list verification (Google Sheet username check): YL’26-specific gate
- Filtered broadcasts (AND-combined field filters with count preview): high manager value
- Scheduled broadcasts: depends on scheduler infra
- Re-registration reminder (DB-tracked, fires once): depends on scheduler infra
- Payment module (for conferences): receipt upload, status flow, tinder review, auto-reminders T-3/T-1
- Bulk “Approve all” with confirmation dialog: eliminates high-volume approval bottleneck

**Defer (out of scope for this milestone):**
- Gamification task system: coins table is the foundation; mechanics blocked by 8 open design questions
- Role-based access (Delegate/Manager/Admin): does not block current milestone
- Recurring broadcast schedules: single-event season model needs only one-time scheduled broadcasts
- OCR/resume parsing: manager opens file directly; complexity not justified
- Two-way Google Sheets sync: caused prior bot failure; Sheets remains write-only append log

### Architecture Approach

Existing module boundaries are clean and must be preserved. New handlers (coins, approvals, payments) are added as independent router files. New services (coins, scheduler) encapsulate domain logic and call `db.py` for SQL. `db.py` remains a pure SQL layer. Router order in `main.py` becomes: `admin → coins → approvals → payments → registration → user_actions`. The `users` table gains 5–6 new nullable columns via `_ensure_column()`, and 4 new tables are added (`coins`, `scheduled_broadcasts`, `reg_in_progress`, `user_consents`). All module toggles live as rows in the existing `bot_settings` KV table — no code change required to toggle features at runtime.

**Major components:**
1. `handlers/approvals.py` (new) — paginated tinder for pending applications; atomic `UPDATE ... WHERE status='pending'` guard with `rowcount` check prevents double-approval
2. `handlers/payments.py` (new) — payment instructions flow; receipt upload FSM; receipt tinder (reuses tinder pattern); cancels scheduler reminders on payment confirmation
3. `services/scheduler.py` (new) — APScheduler wrapper; exposes `schedule_broadcast()`, `schedule_reminder()`, `cancel_job()`; loads pending jobs from DB on startup
4. `services/coins.py` (new) — `add_transaction()`, `get_balance()` (computed as `SUM(delta)`), `get_leaderboard()`; never reads-modifies-writes a balance field
5. `database/db.py` (extended) — new `_ensure_column` calls; 4 new `CREATE TABLE IF NOT EXISTS` blocks; new query functions for pending applications, coin transactions, broadcast jobs

### Critical Pitfalls

1. **Non-atomic coins balance (the prior bot failure)** — Never use read-modify-write on a balance column. Use INSERT-only `coins` ledger; compute balance as `SUM(delta)`. For spend operations that check then deduct, wrap in `BEGIN IMMEDIATE` transaction within a single aiosqlite connection.

2. **`INSERT OR REPLACE` destroys `status` on re-registration** — SQLite REPLACE is DELETE + INSERT; all new fields reset to defaults. Switch `add_user()` to `INSERT ... ON CONFLICT(telegram_id) DO UPDATE SET` listing only fields to overwrite, explicitly excluding `status` and `payment_status`. Must be done the moment `status` is added to the users table.

3. **Broadcast missing `TelegramRetryAfter` retry** — Current `sleep(0.05)` loop has no retry; rate-limited users are counted as “blocked.” Add a `TelegramRetryAfter` catch with `sleep(e.retry_after + 1)` before adding any scheduled or filtered broadcasts on top of this infrastructure.

4. **Concurrent manager double-approval** — Two managers can act on the same application simultaneously. Use atomic `UPDATE users SET status='approved' WHERE telegram_id=? AND status='pending'` and check `cursor.rowcount == 0` to silently skip if already processed. A Python-level status check followed by a separate UPDATE has a TOCTOU window.

5. **Approval queue pagination and dropout tracking in MemoryStorage** — MemoryStorage resets on restart. Approval queue pagination must be driven by DB query (oldest `status=pending` row), not an FSM offset. Dropout tracking must write to `reg_in_progress` DB table on `/start` and delete on completion — FSM state cannot be enumerated or persisted across restarts.

---

## Implications for Roadmap

Based on the dependency graph in FEATURES.md and the build order in ARCHITECTURE.md, research supports a 4-phase structure with clear unlock relationships between phases.

### Phase 1: DB Foundation + Quick Wins

**Rationale:** DB schema migrations are safe to deploy against production immediately (`DEFAULT 'approved'` on `status` means all existing users pass `ensure_registered()` unchanged). Quick-win features (coins, channel gate) are purely additive, do not touch the registration flow, and deliver visible value before the heavier approval flow work begins. Establishing the coins ledger correctly now prevents the known data-loss bug before any data accumulates.

**Delivers:** New DB schema live in production; coins economy with audit trail; channel subscription gate; registration confirmation step

**Features addressed:** Coins transactions log, leaderboard, `/coins` admin command, `/рейтинг` user command, channel subscription gate, resume file upload, registration confirmation step (uses existing `get_confirm_kb()`)

**Pitfalls to prevent:** Non-atomic coins (Pitfall 1), audit `add_user()` for INSERT OR REPLACE even before approval flow is built (Pitfall 2), getChatMember without in-process cache (Pitfall 9), schema migration without versioning (Pitfall 8)

**New files:** `services/coins.py`, `handlers/coins.py`

---

### Phase 2: Approval Flow (Core Milestone)

**Rationale:** Central milestone requirement. Must come after Phase 1 DB schema is live (needs `status` column) and after the registration confirmation step is stable — modifying `finalize_registration()` while simultaneously adding a confirmation step creates conflicts in `registration.py`. Everything that reads `status=approved` — `ensure_registered()`, the payment module, filtered broadcasts — depends on this phase completing first.

**Delivers:** Full moderated registration path: applications go to `status=pending`, managers review via paginated tinder UI, users are notified on approve/reject. `ensure_registered()` checks `status=approved`.

**Features addressed:** `status` field, `submit_application()` split from `finalize_registration()`, tinder UI with pagination (card N of M, edit-in-place via `message.edit_text`), approve/reject/skip/approve-all buttons, per-form moderation toggles (`short_approval`/`full_approval`), manager pending-queue periodic reminder

**Pitfalls to prevent:** Concurrent double-approval via atomic UPDATE + rowcount (Pitfall 6), pagination state in MemoryStorage (Pitfall 7 — paginate by oldest `status=pending` DB row), admin notification storm (Pitfall 12 — periodic scheduled reminder replaces per-submission push), INSERT OR REPLACE on `add_user()` (Pitfall 2 — must be fixed before re-registration of approved users is possible)

**New files:** `handlers/approvals.py`; changes to `registration.py`, `user_actions.py`, `admin.py`, `states.py`, `keyboards/builders.py`

---

### Phase 3: Scheduler Infrastructure + Communications

**Rationale:** APScheduler is a cross-cutting prerequisite for four features (scheduled broadcasts, re-registration reminders, payment auto-reminders, manager queue reminders). Setting it up as one infrastructure investment before building any dependent feature is more efficient. Scheduled broadcasts depend on approval flow being stable (approval status is a filter target). Re-registration reminders depend on `reg_in_progress` writes in `registration.py`, which stabilize during Phase 2.

**Delivers:** Persistent broadcast scheduler; filtered and scheduled broadcasts; re-registration dropout reminders; pre-selected list verification gate; manager pending-queue reminders via scheduler

**Features addressed:** APScheduler setup (see persistence decision below), filtered broadcasts (AND-combined DB field filters + count preview before send), scheduled broadcasts, re-registration reminders (fires once per dropout), pre-selected list verification (Google Sheet column check on `/start`)

**Pitfalls to prevent:** Scheduler jobs lost on restart (Pitfall 3 — DB table + startup restore), timezone bug in date parsing (Pitfall 4 — localize all dates to `Europe/Moscow` before passing to APScheduler), duplicate APScheduler jobs on restart (Pitfall 13 — always use `id=` + `replace_existing=True`), broadcast rate limiting (Pitfall 5 — `TelegramRetryAfter` retry must be in place before scheduled broadcasts add volume)

**New files:** `services/scheduler.py`; changes to `admin.py`, `registration.py`, `states.py`

---

### Phase 4: Universal Modules (Payment + Event Modularity)

**Rationale:** The payment module is the highest-complexity feature (multiple status transitions, receipt tinder, per-user reminder jobs with cancellation, penalty schedule display). It depends on: approval flow (`status=approved` triggers payment instructions), scheduler (T-3/T-1 jobs), and event modularity toggle (`module_payment=on`). Building it last ensures all prerequisites are stable and the tinder UI pattern is proven. The event modularity admin UI is most useful when all the modules it controls already exist.

**Delivers:** Full conference-ready payment flow; configurable consent module injected into REG_FLOW; event type + module toggle admin UI; new `date` and `consent` REG_FLOW step types

**Features addressed:** Payment module (instructions, receipt upload FSM, admin receipt tinder, T-3/T-1 reminders, overdue status), consent module (configurable list from `bot_settings`, each consent requires explicit acceptance), event type toggle (`forum`/`conference`/`custom`), `date` question type with DD.MM.YYYY validation, `consent` question type, re-receipt upload after manager rejection, cancellation penalty scale display

**Pitfalls to prevent:** Payment reminder sent before user has seen payment instructions (gate on `receipt_instructions_sent` flag), file_id invalidation on token rotation (Pitfall 11 — document the limitation; optionally store `local_path` alongside `file_id`), receipt MIME type validation (reject non-PDF by MIME content, not filename), timezone-aware date parsing for payment deadlines (Pitfall 4)

**New files:** `handlers/payments.py`; changes to `admin.py`, `states.py`, `keyboards/builders.py`

---

### Phase Ordering Rationale

- **DB first** because all other phases are blocked by table/column availability; can be deployed in isolation without touching any handler logic
- **Coins before approval flow** because coins are purely additive and must be correct from first use to prevent the known data-loss bug
- **Approval flow before scheduler** because the approval reminder job queries `status=pending` and filtered broadcasts filter on `status` — both need the approval schema stable
- **Scheduler before payment** because per-user payment deadline reminder jobs (T-3/T-1) require the scheduler service to be proven reliable before user-facing deadline jobs are created
- **Payment last** because it depends on all three prior phases and is the highest-complexity module; building it last minimizes rework risk

This ordering aligns with both the FEATURES.md MVP phasing (Phase 1/2/3/4) and the ARCHITECTURE.md recommended build order (Steps 1–10).

---

### APScheduler Persistence — Open Decision (Confirm Before Phase 3 Planning)

STACK.md and PITFALLS.md recommend `SQLAlchemyJobStore`; ARCHITECTURE.md recommends `MemoryJobStore` + `scheduled_broadcasts` table + startup restore. Both are valid. The tradeoff:

**Option A: `SQLAlchemyJobStore` (STACK.md + PITFALLS.md recommendation)**
- Pros: APScheduler handles persistence natively; no manual restore code to write or maintain; battle-tested
- Cons: Adds `sqlalchemy>=2.0` to a pure-aiosqlite codebase; job payloads stored as opaque pickles (hard to inspect/repair); SQLAlchemy I/O is synchronous (negligible at <50 jobs but architecturally inconsistent with the rest of the stack)

**Option B: `MemoryJobStore` + `scheduled_broadcasts` DB table + startup restore (ARCHITECTURE.md recommendation)**
- Pros: No new ORM dependency; all job metadata fully readable as SQL; consistent with existing aiosqlite pattern; PITFALLS.md notes opaque pickle payloads are hard to repair in production
- Cons: Requires ~30 lines of startup restore code; one more thing to maintain; must handle `misfire_grace_time` edge cases correctly

**Recommendation:** Option B (MemoryJobStore + DB restore). The project is explicitly ORM-free; adding SQLAlchemy for one peripheral feature is inconsistent with that stance. The restore code is simple and fully specified in ARCHITECTURE.md. The `scheduled_broadcasts` table also provides better operational visibility than pickle-stored APScheduler job payloads.

**This is a decision to confirm during Phase 3 planning, not a blocker for Phases 1 or 2.**

---

### Research Flags

**Phases with standard patterns (no additional research needed):**
- **Phase 1 (DB + Quick Wins):** All patterns are in production code or fully specified in research. `_ensure_column` is already deployed; coins ledger is a straightforward INSERT pattern with no edge cases.
- **Phase 2 (Approval Flow):** Tinder UI pattern, atomic UPDATE guard, and `_send_approved_content` extraction are fully specified in ARCHITECTURE.md with exact code shapes. No implementation unknowns.
- **Phase 3 (Scheduler):** APScheduler 3.x `AsyncIOScheduler` patterns are well-documented. Filtered broadcast SQL is standard. The one open question (Option A vs B) is an architectural choice, not a research gap.

**Phases that may benefit from targeted research during planning:**
- **Phase 4 (Payment Module):** The per-user deadline reminder job cancellation flow (behavior when a user uploads a new receipt after a reminder was already scheduled) and the cancellation penalty configuration format have some implementation ambiguity. Recommend a light research pass before Phase 4 planning focused on APScheduler job cancellation/replacement patterns and receipt review tinder FSM edge cases.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All libraries verified against PyPI and official docs; version constraints confirmed. The sqlalchemy dependency question is an architectural choice, not a research gap. |
| Features | HIGH | Derived directly from PROJECT.md, PLAN_YOULEAD_TZ.md, and existing codebase. No speculative features. Dependencies and priorities are explicit. |
| Architecture | HIGH | Based on direct code inspection of the production codebase. Component boundaries, router order, DB schema, and build sequence fully specified with rationale. |
| Pitfalls | HIGH | Critical pitfalls traced to real production failures and specific lines in the existing code (INSERT OR REPLACE in db.py, sleep(0.05) in admin.py). Prevention patterns are concrete and actionable. |

**Overall confidence:** HIGH

### Gaps to Address

- **APScheduler persistence (Option A vs B):** Confirm before Phase 3 begins. Does not block Phases 1 or 2. See reconciliation above.
- **Pre-selected list sheet structure:** The verification gate reads a Google Sheets column for Telegram usernames. The exact sheet name, tab, and column for YL’26 must be confirmed with the AIESEC manager before Phase 3 implementation begins.
- **Payment cancellation scope:** PROJECT.md mentions a cancellation penalty scale but does not specify whether the bot should handle user-initiated cancellation requests or only display the fee schedule. Confirm before Phase 4 planning to prevent mid-phase scope change.
- **Consent texts for YL’26:** The consent module is configuration-driven; the actual consent texts (data processing, photo/video rights, event rules) must be provided by the organizer. Code gap: none. Content gap: yes, needs organizer input.

---

## Sources

### Primary (HIGH confidence)
- `handlers/registration.py`, `database/db.py`, `handlers/admin.py`, `handlers/states.py`, `main.py` — direct code inspection, source of truth for existing patterns
- `PROJECT.md` — active scope, constraints, key decisions
- `PLAN_YOULEAD_TZ.md` — UI mockups, known failure modes, architecture decisions
- Context7 `/agronholm/apscheduler` — APScheduler 3.x SQLAlchemyJobStore, AsyncIOScheduler, misfire_grace_time patterns
- Context7 `/websites/aiogram_dev_en_v3_27_0` — getChatMember, ChatMemberStatus, MEMBERS group, document file_id, TelegramRetryAfter
- APScheduler PyPI — confirmed 3.11.2 stable, 4.0.0a6 pre-release as of June 2026
- SQLAlchemy PyPI / changelog — confirmed 2.0.51 stable as of June 2026
- Telegram Bot API docs — getChatMember rate limits, 30 msg/sec global limit, file_id stability per token

### Secondary (MEDIUM confidence)
- aiogram community patterns — `AsyncIOScheduler` as standard for async bots, tinder pagination via callback_data
- SQLite docs — `INSERT OR REPLACE` semantics (DELETE + INSERT), WAL mode, `BEGIN IMMEDIATE`, `PRAGMA user_version`

---
*Research completed: 2026-06-25*
*Ready for roadmap: yes*
