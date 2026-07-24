# Phase 3: Scheduler + Communications + Verification - Context

**Gathered:** 2026-06-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 3 delivers three capabilities on top of the existing admin/broadcast/registration code:

1. **Persistent scheduling** — admin can schedule a filtered broadcast for a future datetime; the job survives bot restart (APScheduler + SQLAlchemyJobStore).
2. **Filtered/flood-safe communications** — admin builds AND-combined audience filters (city/uni/status/source/reg-date), sees a count preview, sends; the send loop survives Telegram 429 rate limits.
3. **Pre-selection gating** — at `/start`, a TG username not in a Google-Sheet allowlist sees "отбор не пройден" + external link; usernameless users get a prompt + manual telegram_id allowlist fallback.

Plus SCHED-03: a one-shot ~2h-inactivity dropout nudge over the Phase-1 `reg_started` table.

**Requirements (8):** COMM-01, COMM-02, COMM-03, COMM-04, SCHED-01, SCHED-03, VERIF-01, VERIF-02.
**Out of scope:** payment/consent reminders (Phase 4 reuses this scheduler), event-type module toggles (Phase 4), SCHED-02 `reg_started` table itself (delivered Phase 1).

</domain>

<decisions>
## Implementation Decisions

### Scheduler backend (SCHED-01)
- **D-01:** APScheduler 3.x `AsyncIOScheduler` + `SQLAlchemyJobStore` (SQLite) owns timing, persistence, and startup restore — per CLAUDE.md tech-stack lock. Resolves the CLAUDE.md-vs-ROADMAP conflict: the two are not exclusive.
- **D-02:** A thin `scheduled_broadcasts` DB table holds the **payload** (message text/photo file_id, filter spec, status sent/pending, scheduled datetime). The persisted APScheduler job stores only `broadcast_id` in its args and calls a module-level `send_scheduled_broadcast(broadcast_id)` that reads the row. This keeps job args small/serializable and makes pending broadcasts listable/cancellable in the admin UI.
- **D-03:** Scheduler is started in `main.py` startup (alongside the existing `pending_reminder_loop` task pattern) and `jobstore` restores jobs automatically on boot.

### Filtered broadcast UX (COMM-01..03)
- **D-04:** New "🎯 По фильтру" entry added to the **existing** broadcast menu (`handlers/admin.py:769` / `cmd_broadcast`). Reuses the `Broadcast` FSM and the `process_broadcast` send path.
- **D-05:** Step-by-step inline AND-builder: pick field → pick value → "добавить ещё фильтр" or "показать N и отправить". Filterable fields: city, university, status, source, registration-date (after/before). Filters combine with AND.
- **D-06:** Matching user **count preview** is shown before send (ROADMAP success-criteria #2). Count comes from a DB query that materializes the filtered `telegram_id` list.

### Flood-safe send loop (COMM-04)
- **D-07:** The current `process_broadcast` loop (`admin.py:1020`) uses a bare `except` + `sleep(0.05)` and has **no 429 handling** — must be upgraded to catch `TelegramRetryAfter`, wait `retry_after + 1`, and retry the same user. Reuse the 429-safe pattern already written in Phase 2 `_welcome_flipped`.
- **D-08:** Rate-limited users that eventually succeed after the wait are **not** counted as blocked; only genuine failures (bot blocked, chat not found) increment the blocked counter.

### Pre-selection gating (VERIF-01/02)
- **D-09:** Allowlist of selected usernames lives in a **separate worksheet/tab** of the existing spreadsheet (tab name configurable via a `bot_settings` key), single username column. `sheet1` stays the registered-users export target, untouched. Selection happens *before* registration, so reusing sheet1 is semantically wrong.
- **D-10:** Username match is normalized: strip leading `@`, lowercase, trim whitespace on both sides.
- **D-11:** The allowlist is **cached in memory** (a `set`) loaded at startup + periodic refresh (reuse `asyncio.to_thread` like `services/sheets.py`) — never a gspread call on every `/start` (latency + quota). Provide an admin refresh trigger.
- **D-12:** VERIF-02 usernameless users: show a prompt to set a Telegram username, **plus** a manual `telegram_id` allowlist (a `bot_settings` key or small table) so edge cases can be admitted without a username.
- **D-13:** Gating is conditional on a setting (pre-selection on/off); when off, `/start` behaves as today. Default off to protect existing live flow.

### Dropout nudge (SCHED-03)
- **D-14:** APScheduler **interval job** (every ~15 min) scans `reg_started` for rows with `started_at` older than ~2h and `nudged_at IS NULL`, sends exactly one nudge, then sets `nudged_at` (one-shot dedup). Same scheduler as SCHED-01 — no second scheduling mechanism.
- **D-15:** Add `nudged_at TEXT` to `reg_started` via the existing `_ensure_column` additive-migration pattern. The `reg_in_progress` name in ROADMAP success-criteria #3 refers to this **existing Phase-1 `reg_started` table** — do NOT create a duplicate table.
- **D-16:** Already-registered users are never nudged: completion already `clear_reg_started()`s the row (Phase 1), so the scan naturally excludes them. The 2h threshold and interval should be settings-driven.

### Claude's Discretion
- Exact refresh interval for the username-allowlist cache, the 2h inactivity threshold default, and the ~15min nudge-scan interval — pick sane defaults, expose as `bot_settings` keys.
- Filter-spec serialization format stored in `scheduled_broadcasts` (JSON blob vs columns) — planner's call.
- Whether the manual telegram_id allowlist (D-12) is a setting string or a tiny table.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Tech-stack locks (MANDATORY)
- `CLAUDE.md` § "New Libraries to Add" / "What NOT to Use" — APScheduler 3.11.2 + `AsyncIOScheduler` + `SQLAlchemyJobStore` (SQLite); sqlalchemy>=2.0; **no** APScheduler 4.0, **no** MemoryJobStore, **no** BackgroundScheduler, **no** custom job table. getChatMember built-in. file_id storage pattern.

### Phase scope
- `.planning/ROADMAP.md` § "Phase 3" — goal, 5 success criteria, requirement list.
- `.planning/REQUIREMENTS.md` — COMM-01..04, SCHED-01, SCHED-03, VERIF-01, VERIF-02 text.
- `.planning/phases/02-approval-flow/02-CONTEXT.md` — Phase 2 decisions (status field, 429-safe send pattern) that Phase 3 depends on.

### No external ADRs
- No separate ADR/spec docs beyond the above — decisions are captured in this file.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `handlers/admin.py:769` `show_admin_broadcast` / `:801` `cmd_broadcast` — existing broadcast target menu; add "🎯 По фильтру" entry here.
- `handlers/admin.py:870` `_start_segment_broadcast` — segment→FSM handoff helper; filtered broadcast can reuse this shape (build user_ids → set Broadcast.message state).
- `handlers/admin.py:1020` `process_broadcast` send loop — the loop to harden for COMM-04 (currently bare except, no 429).
- Phase 2 `_welcome_flipped` (admin.py) — already implements `TelegramRetryAfter` catch + `retry_after+1` wait; copy this for D-07.
- `services/reminders.py` `pending_reminder_loop` — proven startup-asyncio-task pattern; the model for wiring the APScheduler start in `main.py`, and contrast for why SCHED-03 uses APScheduler instead.
- `services/sheets.py` — `_get_sheet()` (sheet1), `asyncio.to_thread` wrapper pattern; VERIF allowlist reader added here for a **different tab**.
- `database/db.py:403-424` `mark_reg_started` / `clear_reg_started` / pending list — `reg_started` table helpers; SCHED-03 adds a scan + `nudged_at` update.

### Established Patterns
- `_ensure_column` additive migrations (db.py) — use for `reg_started.nudged_at` and any `scheduled_broadcasts` table (`CREATE TABLE IF NOT EXISTS`).
- `bot_settings` key-value store (`get_setting`/`set_setting`) — all toggles/thresholds (pre-selection on/off, allowlist tab name, intervals).
- `Broadcast` FSM (`handlers/states.py`) — extend with filter-builder states.

### Integration Points
- `main.py` startup — instantiate + start `AsyncIOScheduler` with `SQLAlchemyJobStore`, then `scheduler.start()` before `start_polling`; jobstore auto-restores SCHED-01 jobs.
- `cmd_start` (`handlers/registration.py`) — VERIF-01 gate inserted at the top, after the rejected-status guard, conditional on the pre-selection setting.

</code_context>

<specifics>
## Specific Ideas

- Reconciled the CLAUDE.md ("no custom job table") vs ROADMAP ("scheduled_broadcasts table") tension by splitting roles: APScheduler = *trigger persistence*, `scheduled_broadcasts` = *payload store keyed by id*. Neither doc is violated.
- Pre-selection allowlist tab informally referred to as "Отобранные".

</specifics>

<deferred>
## Deferred Ideas

- Payment-deadline reminders (T-3/T-1 days) — Phase 4 (PAY-06), reuses this APScheduler service.
- Consent module + event-type/module toggles — Phase 4 (MOD/CONS).
- Recurring/cron-style broadcasts — not requested; one-time `date` triggers only this phase.

</deferred>

---

*Phase: 3-scheduler-communications-verification*
*Context gathered: 2026-06-27*
