# Phase 3: Scheduler + Communications + Verification - Research

**Researched:** 2026-06-27
**Domain:** Persistent async job scheduling (APScheduler 3.x) + flood-safe Telegram broadcasts + Google-Sheet allowlist gating, all integrated additively into a live aiogram 3 / aiosqlite brownfield bot
**Confidence:** HIGH (all stack versions verified against the installed environment; all integration points read from current source)

## Summary

Phase 3 adds three capabilities to an existing, production aiogram-3 bot without touching the locked core stack. The hard part is not any single feature — it is wiring **APScheduler 3.x** (`AsyncIOScheduler` + `SQLAlchemyJobStore`) into an event loop that is already owned by aiogram long-polling, and doing so without (a) corrupting the live `forum.db` through two concurrent SQLite writers, or (b) accidentally adopting the APScheduler **4.0** API that every current documentation source (including Context7) now returns by default. Both of those are real, verified traps documented below.

The other two features are lower-risk because the codebase already contains the exact patterns to copy: the **429-safe send loop** exists verbatim in `handlers/admin.py:1380` (`_welcome_flipped`), and the **`asyncio.to_thread` gspread wrapper** exists in `services/sheets.py`. The filtered-broadcast UX reuses the existing `Broadcast` FSM and the `_start_segment_broadcast` → `process_broadcast` send path. The dropout nudge reuses the Phase-1 `reg_started` table (just add one column). Verification gating slots into `cmd_start` at one well-defined insertion point.

**Primary recommendation:** Build a single `services/scheduler.py` module that owns a module-level `AsyncIOScheduler` and a module-level `Bot` reference (set once at startup), exposing importable module-level job functions (`send_scheduled_broadcast(broadcast_id)`, `nudge_incomplete_registrations()`). Point its `SQLAlchemyJobStore` at a **separate** SQLite file (`data/jobs.sqlite`), never at `forum.db`. Keep all broadcast *payloads* in a `scheduled_broadcasts` table in `forum.db` (aiosqlite); the persisted job carries only `broadcast_id`. Pin `apscheduler==3.11.2`, `sqlalchemy>=2.0,<3.0` (2.0.49 installed). Reuse `_welcome_flipped`'s `TelegramRetryAfter` pattern for COMM-04 and the `services/sheets.py` `to_thread` pattern for the allowlist reader.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Scheduler backend (SCHED-01)**
- **D-01:** APScheduler 3.x `AsyncIOScheduler` + `SQLAlchemyJobStore` (SQLite) owns timing, persistence, and startup restore — per CLAUDE.md tech-stack lock. Resolves the CLAUDE.md-vs-ROADMAP conflict: the two are not exclusive.
- **D-02:** A thin `scheduled_broadcasts` DB table holds the **payload** (message text/photo file_id, filter spec, status sent/pending, scheduled datetime). The persisted APScheduler job stores only `broadcast_id` in its args and calls a module-level `send_scheduled_broadcast(broadcast_id)` that reads the row. Keeps job args small/serializable and makes pending broadcasts listable/cancellable in the admin UI.
- **D-03:** Scheduler started in `main.py` startup (alongside the existing `pending_reminder_loop` task pattern); `jobstore` restores jobs automatically on boot.

**Filtered broadcast UX (COMM-01..03)**
- **D-04:** New "🎯 По фильтру" entry added to the **existing** broadcast menu (`handlers/admin.py:769` / `cmd_broadcast`). Reuses the `Broadcast` FSM and the `process_broadcast` send path.
- **D-05:** Step-by-step inline AND-builder: pick field → pick value → "добавить ещё фильтр" or "показать N и отправить". Filterable fields: city, university, status, source, registration-date (after/before). Filters combine with AND.
- **D-06:** Matching user **count preview** shown before send (ROADMAP success-criteria #2). Count comes from a DB query that materializes the filtered `telegram_id` list.

**Flood-safe send loop (COMM-04)**
- **D-07:** The current `process_broadcast` loop (`admin.py:1020`) uses a bare `except` + `sleep(0.05)` and has **no 429 handling** — must be upgraded to catch `TelegramRetryAfter`, wait `retry_after + 1`, and retry the same user. Reuse the 429-safe pattern already written in Phase 2 `_welcome_flipped`.
- **D-08:** Rate-limited users that eventually succeed after the wait are **not** counted as blocked; only genuine failures (bot blocked, chat not found) increment the blocked counter.

**Pre-selection gating (VERIF-01/02)**
- **D-09:** Allowlist of selected usernames lives in a **separate worksheet/tab** of the existing spreadsheet (tab name configurable via a `bot_settings` key), single username column. `sheet1` stays the registered-users export target, untouched.
- **D-10:** Username match normalized: strip leading `@`, lowercase, trim whitespace on both sides.
- **D-11:** Allowlist **cached in memory** (a `set`) loaded at startup + periodic refresh (reuse `asyncio.to_thread` like `services/sheets.py`) — never a gspread call on every `/start`. Provide an admin refresh trigger.
- **D-12:** VERIF-02 usernameless users: show a prompt to set a Telegram username, **plus** a manual `telegram_id` allowlist (a `bot_settings` key or small table) so edge cases can be admitted without a username.
- **D-13:** Gating conditional on a setting (pre-selection on/off); when off, `/start` behaves as today. Default off to protect existing live flow.

**Dropout nudge (SCHED-03)**
- **D-14:** APScheduler **interval job** (every ~15 min) scans `reg_started` for rows with `started_at` older than ~2h and `nudged_at IS NULL`, sends exactly one nudge, then sets `nudged_at` (one-shot dedup). Same scheduler as SCHED-01.
- **D-15:** Add `nudged_at TEXT` to `reg_started` via the existing `_ensure_column` additive-migration pattern. The `reg_in_progress` name in ROADMAP success-criteria #3 refers to this **existing Phase-1 `reg_started` table** — do NOT create a duplicate table.
- **D-16:** Already-registered users are never nudged: completion already `clear_reg_started()`s the row (Phase 1), so the scan naturally excludes them. The 2h threshold and interval should be settings-driven.

### Claude's Discretion
- Exact refresh interval for the username-allowlist cache, the 2h inactivity threshold default, and the ~15min nudge-scan interval — pick sane defaults, expose as `bot_settings` keys.
- Filter-spec serialization format stored in `scheduled_broadcasts` (JSON blob vs columns) — planner's call.
- Whether the manual telegram_id allowlist (D-12) is a setting string or a tiny table.

### Deferred Ideas (OUT OF SCOPE)
- Payment-deadline reminders (T-3/T-1 days) — Phase 4 (PAY-06), reuses this APScheduler service.
- Consent module + event-type/module toggles — Phase 4 (MOD/CONS).
- Recurring/cron-style broadcasts — not requested; one-time `date` triggers only this phase.
- SCHED-02 `reg_started` table itself — delivered Phase 1.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| COMM-01 | Filtered broadcast — inline menu over DB fields (city, ВУЗ, status, source) | Reuse `Broadcast` FSM + new filter-builder states; dynamic `WHERE` query (Pattern 3). Fields confirmed present in `users` table. |
| COMM-02 | Filter by registration date (after/before) | `registration_date` stored as `"%Y-%m-%d %H:%M:%S"` (verified `registration.py:969`) → lexicographic string compare is valid: `registration_date >= '2026-06-01'`. |
| COMM-03 | AND-combine filters | Build `WHERE` clause from a list of (field, op, value) tuples joined by `AND`; parameterized (Pitfall 5). |
| COMM-04 | Flood-safe (429 handling, retries, block accounting) | Copy `_welcome_flipped` pattern (`admin.py:1380`) into `process_broadcast`. `TelegramRetryAfter.retry_after` shape verified in installed aiogram 3.24.0. |
| SCHED-01 | Delayed broadcasts survive restart (persistent store) | `AsyncIOScheduler` + `SQLAlchemyJobStore` `date` trigger; `add_job` signature verified on installed 3.11.2. Payload in `scheduled_broadcasts` (D-02). |
| SCHED-03 | Auto-nudge incomplete registrations | Interval job over `reg_started` + new `nudged_at` column; table + helpers confirmed present (`db.py:403-425`). |
| VERIF-01 | `/start` username vs Google-Sheet allowlist gate | New tab reader via `to_thread` (Pattern 4); in-memory set cache; gate inserted in `cmd_start` (`registration.py:553`). |
| VERIF-02 | Usernameless users — prompt + manual telegram_id allowlist | `message.from_user.username is None` branch → prompt + manual-id set check. |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Job timing + persistence + restart restore (SCHED-01/03) | Scheduler service (`services/scheduler.py`, APScheduler) | `jobs.sqlite` (sync SQLAlchemy) | Trigger persistence is APScheduler's job; keep it in its own store/file. |
| Broadcast payload storage (D-02) | Application DB (`forum.db` via aiosqlite) | — | Payload is app data, listable/cancellable in admin UI; belongs with the rest of the schema. |
| Audience filtering + count (COMM-01..03) | Application DB query (`database/db.py`) | Admin handler (FSM builder) | Filtering is a SQL concern; the handler only assembles the filter spec. |
| Flood-safe delivery (COMM-04) | Admin handler send loop (`handlers/admin.py`) | aiogram exceptions | Delivery + rate-limit handling is a Telegram-client concern. |
| Allowlist source-of-truth | Google Sheet (separate tab) | `to_thread` reader in `services/` | Selection list is owned by DXP managers in Sheets; bot only reads/caches. |
| Allowlist hot-path lookup (VERIF-01) | In-memory `set` cache | `bot_settings` (manual-id fallback) | `/start` must not call gspread (latency + quota); cache in process. |
| Gating decision at `/start` | Registration handler (`cmd_start`) | `bot_settings` toggle | Conditional flow control belongs at the entry handler. |

## Standard Stack

### Core (all already installed — verified in this environment)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| apscheduler | **3.11.2** | Persistent async job scheduling (`date` + `interval` triggers) | CLAUDE.md lock; 4.0 still alpha. `AsyncIOScheduler`+`SQLAlchemyJobStore` imports verified working. `[VERIFIED: pip + python import]` |
| sqlalchemy | **2.0.49** (lock `>=2.0,<3.0`) | Backend for `SQLAlchemyJobStore` (sync engine) | Required by APScheduler's SQLite jobstore. Installed & importable. `[VERIFIED: python -c import]` |
| aiogram | **3.24.0** (installed) | Telegram framework — already in use | `TelegramRetryAfter` exception present; `.retry_after` attr used in live code. `[VERIFIED: import + admin.py:1385]` |
| aiosqlite | latest | Async driver for `forum.db` (all app queries) | Existing; unchanged. APScheduler does NOT use it. |
| gspread + google-auth | latest | Read allowlist tab + existing sheet export | Existing `services/sheets.py` pattern. |

> **Version note (MEDIUM):** CLAUDE.md cites aiogram 3.29.0 as "latest stable," but the environment has **3.24.0** installed. No upgrade is required for this phase — `TelegramRetryAfter` and `send_copy` exist in 3.24. Do not bump aiogram as part of Phase 3 unless a specific 3.25+ API is needed (none identified). `[VERIFIED: aiogram.__version__ == 3.24.0]`

### Supporting (no new libraries)
| Mechanism | Where | Purpose |
|-----------|-------|---------|
| `_ensure_column` additive migration | `database/db.py:17` | Add `reg_started.nudged_at TEXT` |
| `CREATE TABLE IF NOT EXISTS` | `database/db.py` `init_db` | New `scheduled_broadcasts` table |
| `bot_settings` k/v (`get_setting`/`set_setting`) | `database/db.py:109` | All toggles/thresholds/tab-name/manual-id list |
| `asyncio.to_thread` gspread wrapper | `services/sheets.py` | Allowlist tab read off the event loop |
| `Broadcast` FSM | `handlers/states.py:45` | Extend with filter-builder states |

### Alternatives Considered (already settled by CLAUDE.md — do NOT re-open)
| Instead of | Rejected alternative | Why rejected |
|------------|----------------------|--------------|
| APScheduler 3.11.2 | APScheduler 4.0 (`AsyncScheduler`/`add_schedule`) | Alpha; **incompatible API** — and is what docs now return (Pitfall 1). |
| `SQLAlchemyJobStore` | `MemoryJobStore` | Jobs lost on restart — violates SCHED-01. |
| `AsyncIOScheduler` | `BackgroundScheduler` | Thread context can't `await bot.send_message`. |
| `_ensure_column` | Alembic / yoyo | ORM/sync overkill for a 1-column add. |
| Separate `jobs.sqlite` | APScheduler jobstore on `forum.db` | Two writers on one file → `database is locked` (Pitfall 2). |

**No installation step required** — `apscheduler` and `sqlalchemy` are already importable. **Add to `requirements.txt`** for reproducibility:
```
apscheduler==3.11.2
sqlalchemy>=2.0,<3.0
```

**Version verification performed (2026-06-27):**
```
apscheduler 3.11.2   (pip show + import OK)
sqlalchemy  2.0.49   (import OK)
aiogram     3.24.0   (import OK)
add_job signature confirmed: (func, trigger, args, id, misfire_grace_time, coalesce, replace_existing, **trigger_args)
```

## Architecture Patterns

### System Architecture Diagram

```
                          ┌────────────────────────────────────────────┐
   /start ───────────────▶│ cmd_start (registration.py:528)            │
                          │  ├─ subscription check (existing)          │
                          │  ├─ [NEW VERIF gate] if preselect=on:       │
   Telegram update         │  │     username norm → in allowlist set?   │──▶ "отбор не пройден" + link
                          │  │     no username → prompt / manual-id     │
                          │  └─ existing register / welcome flow        │
                          └───────────────┬────────────────────────────┘
                                          │ reads
                                          ▼
                              ┌──────────────────────┐    refresh (interval/admin)
                              │ allowlist set (RAM)   │◀────────── to_thread ─────── Google Sheet
                              └──────────────────────┘                              "Отобранные" tab

   admin "🎯 По фильтру" ──▶ Broadcast FSM (filter builder) ──▶ COUNT preview ──▶ confirm
                                          │                                          │
                                          │ build spec                               │ send now
                                          ▼                                          ▼
                              ┌──────────────────────┐                  ┌───────────────────────────┐
   admin "schedule" ─────────▶│ scheduled_broadcasts │                  │ process_broadcast loop    │
                              │  (forum.db, payload) │                  │  send_copy + 429 retry    │──▶ users
                              └──────────┬───────────┘                  │  (COMM-04, copy _welcome) │
                                         │ id only                      └───────────────────────────┘
                                         ▼                                          ▲
   main.py startup ──▶ ┌─────────────────────────────────┐   date trigger fires    │
                       │ AsyncIOScheduler (services/      │─────────────────────────┘
                       │  scheduler.py)                   │   send_scheduled_broadcast(id)
                       │  jobstore = SQLAlchemyJobStore   │   reads row → reuses send loop
                       │   (sqlite:///data/jobs.sqlite)   │
                       │  interval job: nudge scan ───────┼──▶ reg_started (started_at<−2h, nudged_at IS NULL)
                       │  restores jobs on boot           │    send 1 nudge → set nudged_at
                       └─────────────────────────────────┘
```

### Recommended file changes (additive)
```
services/
├── scheduler.py     # NEW: AsyncIOScheduler owner, module-level bot ref, job functions
├── allowlist.py     # NEW (or extend sheets.py): allowlist tab reader + in-RAM set cache
├── sheets.py        # EXTEND: _get_allowlist_tab() via to_thread (NOT sheet1)
└── reminders.py     # unchanged (contrast pattern)
database/db.py       # EXTEND: scheduled_broadcasts table, nudged_at column, filter query, nudge-scan helpers
handlers/admin.py    # EXTEND: "🎯 По фильтру" entry, filter FSM, schedule entry, harden process_broadcast (429)
handlers/registration.py  # EXTEND: VERIF gate in cmd_start
handlers/states.py   # EXTEND: Broadcast filter states (or new FilterBroadcast group)
main.py              # EXTEND: init + start scheduler before start_polling
```

### Pattern 1: Scheduler service with module-level bot injection (THE key HOW gap)

APScheduler's `SQLAlchemyJobStore` **pickles each job by function reference** (`module:qualname`) and pickles its `args`. Therefore: job functions MUST be importable module-level functions (no closures/lambdas/local funcs), and args MUST be picklable primitives. You **cannot** pass the `Bot` object as a job arg. The standard pattern is a module-level bot reference set once at startup.

```python
# services/scheduler.py   [CITED: apscheduler.readthedocs.io/en/3.x/userguide.html + ASSUMED injection pattern]
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_bot = None  # module-level Bot ref — NOT passed as a job arg (unpicklable)

def get_scheduler() -> AsyncIOScheduler:
    return _scheduler

def init_scheduler(bot) -> AsyncIOScheduler:
    """Called once from main.py startup, before start_polling."""
    global _scheduler, _bot
    _bot = bot
    jobstores = {
        # SEPARATE file from forum.db — avoids sync/async SQLite write contention (Pitfall 2)
        "default": SQLAlchemyJobStore(url="sqlite:///data/jobs.sqlite")
    }
    _scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        job_defaults={
            "misfire_grace_time": 3600,  # if bot was down at fire time, still run within 1h (Pitfall 4)
            "coalesce": True,            # collapse multiple missed runs of one job into one
        },
    )
    # interval job for SCHED-03 — replace_existing so a restart doesn't duplicate it
    _scheduler.add_job(
        nudge_incomplete_registrations, "interval", minutes=15,
        id="nudge_scan", replace_existing=True,
    )
    _scheduler.start()  # must be called inside the running asyncio loop
    return _scheduler

async def send_scheduled_broadcast(broadcast_id: int):
    """Module-level job target for SCHED-01. Reads payload row, sends, marks sent."""
    # import here to avoid circulars; read scheduled_broadcasts row from forum.db (aiosqlite)
    ...  # uses _bot

async def nudge_incomplete_registrations():
    """Interval job for SCHED-03. Scans reg_started, sends one nudge, sets nudged_at."""
    ...  # uses _bot
```

**Scheduling a future broadcast (SCHED-01):**
```python
# verified add_job signature on installed 3.11.2
from datetime import datetime
get_scheduler().add_job(
    send_scheduled_broadcast, "date",
    run_date=when,                 # datetime
    args=[broadcast_id],           # picklable primitive ONLY
    id=f"bcast_{broadcast_id}",
    replace_existing=True,
)
```

### Pattern 2: Start ordering in main.py
`scheduler.start()` must run while the asyncio loop is running but before/around `dp.start_polling`. Mirror the existing `asyncio.create_task(pending_reminder_loop(bot))` placement at `main.py:46`.

```python
# main.py — after bot/dp built, before start_polling
from services.scheduler import init_scheduler
init_scheduler(bot)                 # starts AsyncIOScheduler; jobstore auto-restores date jobs
asyncio.create_task(pending_reminder_loop(bot))   # existing (unchanged)
await dp.start_polling(bot)
```
> `AsyncIOScheduler.start()` attaches to the current running loop. Because `main()` is already inside `asyncio.run(main())`, calling `init_scheduler(bot)` here is safe. `[CITED: apscheduler 3.x AsyncIOScheduler docs]`

### Pattern 3: Dynamic AND filter query (COMM-01..03)
Build a parameterized `WHERE` from a validated field whitelist. Never string-format user values.
```python
# database/db.py
_FILTER_COLUMNS = {"city", "university", "status", "source"}  # whitelist — guards against injection

async def count_and_list_filtered(filters: list[dict]) -> list[int]:
    clauses, params = [], []
    for f in filters:
        col = f["field"]
        if col in _FILTER_COLUMNS:
            clauses.append(f"{col} = ?"); params.append(f["value"])
        elif col == "registration_date":
            op = ">=" if f["op"] == "after" else "<"
            clauses.append(f"registration_date {op} ?"); params.append(f["value"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(f"SELECT telegram_id FROM users{where}", params) as cur:
            return [r[0] for r in await cur.fetchall()]
```
The count preview (D-06) is `len(...)` of this list; the same list becomes `target_users` handed to the existing `_start_segment_broadcast` flow.

### Pattern 4: Allowlist tab reader + RAM cache (VERIF-01/11)
```python
# services/sheets.py — NEW, reads a DIFFERENT tab (not sheet1)
def _get_allowlist_rows_sync(tab_name: str) -> list[str]:
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    ws = sh.worksheet(tab_name)          # raises WorksheetNotFound if tab missing — handle
    return ws.col_values(1)              # single username column

# services/allowlist.py — in-RAM cache
_allowlist: set[str] = set()
def _normalize(u: str) -> str:           # D-10
    return u.strip().lstrip("@").lower()
async def refresh_allowlist():
    tab = await get_setting("preselect_tab") or "Отобранные"
    rows = await asyncio.to_thread(_get_allowlist_rows_sync, tab)
    global _allowlist
    _allowlist = {_normalize(v) for v in rows[1:] if v and v.strip()}  # skip header
def is_allowed(username: str | None) -> bool:
    return bool(username) and _normalize(username) in _allowlist
```
Refresh at startup and on an interval (or APScheduler interval job), plus an admin "обновить список" button. `/start` only touches the RAM set.

### Pattern 5: 429-safe send loop (COMM-04) — copy the existing Phase-2 pattern
The exact pattern already lives at `handlers/admin.py:1380` (`_welcome_flipped`). Apply it to `process_broadcast`:
```python
from aiogram.exceptions import TelegramRetryAfter   # already imported admin.py:35
count = blocked = 0
for chat_id in users_ids:
    try:
        await message.send_copy(chat_id)
        count += 1
    except TelegramRetryAfter as e:        # 429 — NOT a block (D-08)
        await asyncio.sleep(e.retry_after + 1)
        try:
            await message.send_copy(chat_id); count += 1
        except Exception:
            blocked += 1
    except Exception:                      # genuine failure (blocked, chat not found)
        blocked += 1
    await asyncio.sleep(0.05)
```

### Anti-Patterns to Avoid
- **Passing `bot`, a coroutine, or a closure as a job arg** — unpicklable → `SQLAlchemyJobStore` raises on add, or the job silently fails to restore. Use module-level functions + `broadcast_id` only.
- **Pointing the jobstore at `forum.db`** — sync SQLAlchemy + async aiosqlite writing the same file → `sqlite3.OperationalError: database is locked`. Use `data/jobs.sqlite`.
- **Calling gspread on every `/start`** — latency + Google quota exhaustion at 1000+ users. Cache in RAM (D-11).
- **String-formatting filter values into SQL** — injection. Whitelist columns, parameterize values (Pattern 3).
- **A second scheduling mechanism for SCHED-03** — reuse the one `AsyncIOScheduler` (D-14), not a new asyncio loop.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Persistent delayed jobs surviving restart | Custom job table + asyncio sleep + recovery loop | APScheduler `SQLAlchemyJobStore` `date` trigger | Serialization, misfire recovery, idempotency, restart restore are all solved & battle-tested. |
| Rate-limit backoff | Manual `sleep(n)` heuristics | Catch `TelegramRetryAfter`, honor `e.retry_after` | Telegram tells you the exact wait; guessing under/over-throttles. |
| Off-loop blocking gspread call | Threads / executors by hand | `asyncio.to_thread` (existing pattern) | One-liner, already proven in `sheets.py`. |
| Schema change | New ORM / migration framework | `_ensure_column` + `CREATE TABLE IF NOT EXISTS` | Project standard; safe against 590 live rows. |
| Username matching | ad-hoc `==` | normalize (strip/`@`/lower) into a `set`, O(1) lookup | Telegram usernames are case-insensitive; `@` optional. |

**Key insight:** Every "hard" part of this phase already has a copyable in-repo precedent or a locked library. The risk is not missing capability — it is the APScheduler 4.0 documentation trap and the dual-writer SQLite trap.

## Runtime State Inventory

> This phase adds NEW persistent state and one schema migration; it is not a rename. Inventory of what must exist/change at runtime:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data (new) | `scheduled_broadcasts` table (payload + status); `reg_started.nudged_at` column | `CREATE TABLE IF NOT EXISTS` in `init_db`; `_ensure_column(db, "reg_started", "nudged_at", "TEXT")`. Both additive, safe. |
| Live service config | New Google Sheet **tab** "Отобранные" (single username column) must exist in the existing spreadsheet; service account must have read access | Manual: DXP manager creates the tab. Tab name stored in `bot_settings.preselect_tab`. Handle `WorksheetNotFound` gracefully (fail-open per D-13 default off). |
| OS-registered state | New file `data/jobs.sqlite` created by APScheduler on first start | None — auto-created. Ensure `data/` is writable (it already holds `forum.db`). Add `data/jobs.sqlite*` to `.gitignore`. |
| Secrets/env vars | None new — reuses `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_FILE`, `BOT_TOKEN` | None. |
| Build artifacts | `requirements.txt` missing `apscheduler`/`sqlalchemy` though both are installed | Add both pins to `requirements.txt` (reproducibility on redeploy). |

**New `bot_settings` keys to introduce (defaults are Claude's discretion per CONTEXT):**
`preselect_enabled` (default `off`), `preselect_tab` (default `Отобранные`), `preselect_fail_text`, `preselect_link`, `preselect_manual_ids` (CSV of telegram_ids — D-12), `nudge_enabled` (default `on`), `nudge_after_minutes` (default `120`), `nudge_scan_minutes` (default `15`), `nudge_text`, `allowlist_refresh_minutes`. All must appear in the D-16 `/settings_guide`.

## Common Pitfalls

### Pitfall 1: Documentation returns the APScheduler 4.0 API (which CLAUDE.md forbids)
**What goes wrong:** Context7 (`/agronholm/apscheduler`), and many current blog posts/StackOverflow answers, now return the **4.0** API: `AsyncScheduler`, `scheduler.add_schedule(...)`, `SQLAlchemyDataStore`, `ConflictPolicy`, `create_async_engine`, `run_until_stopped()`. None of that exists in 3.11.2.
**Verified during this research:** a Context7 docs fetch returned exactly the 4.0 `AsyncScheduler`/`add_schedule`/`SQLAlchemyDataStore` examples. `[VERIFIED: ctx7 fetch 2026-06-27]`
**Correct 3.x API (installed & verified):** `from apscheduler.schedulers.asyncio import AsyncIOScheduler`; `from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore`; `scheduler.add_job(func, "date"|"interval", args=[...], id=..., replace_existing=True, misfire_grace_time=...)`; `scheduler.start()`.
**How to avoid:** Ignore any snippet mentioning `add_schedule`, `AsyncScheduler`, `DataStore`, `conflict_policy`, or `run_until_stopped`. Only use the 3.x readthedocs (`/en/3.x/`) and the verified `add_job` signature in this doc.

### Pitfall 2: Two SQLite writers on one file → `database is locked`
**What goes wrong:** APScheduler's `SQLAlchemyJobStore` uses a **synchronous** SQLAlchemy engine. The app uses **async aiosqlite**. If both target `forum.db`, concurrent writes (a job firing while a user registers) can raise `sqlite3.OperationalError: database is locked`.
**Why it happens:** SQLite locks the whole file for writes; two independent connection pools (sync + async) don't coordinate.
**How to avoid:** Give APScheduler its own file: `sqlite:///data/jobs.sqlite`. Job *triggers* live there; broadcast *payloads* live in `forum.db` via aiosqlite (D-02). They never share a connection. (If ever forced onto one file, enable WAL — but separate files is simpler and recommended.)
**Warning signs:** intermittent `OperationalError` in logs around job-fire times.

### Pitfall 3: Job function not importable / closure → restore fails silently
**What goes wrong:** A persisted job is stored as `module:function` + pickled args. If the target is a lambda, a nested function, or moves module path, restore after restart raises `LookupError`/`ImportError` and the job vanishes.
**How to avoid:** Keep job targets as stable top-level functions in `services/scheduler.py`. Args = primitives only (`broadcast_id: int`). Never refactor a scheduled job's module path without a migration plan.
**Warning signs:** scheduled broadcast silently doesn't fire after a restart; log shows job removed on load.

### Pitfall 4: Misfire after downtime drops the broadcast
**What goes wrong:** A `date` job whose `run_date` passed while the bot was down is, by default, discarded if it missed by more than `misfire_grace_time` (default small).
**How to avoid:** Set a generous `misfire_grace_time` (e.g., 3600s) in `job_defaults` so a broadcast scheduled during a brief outage still fires on reboot; `coalesce=True` prevents duplicate runs of the interval scan after a long outage. Decide the product rule: should a broadcast whose time passed during a 6h outage still send? (Open Question 1.)

### Pitfall 5: Filter-value SQL injection
**What goes wrong:** Building `WHERE city = '{value}'` from admin-typed input. Even admin-only input should be parameterized (defense in depth; values may originate from sheet/user data).
**How to avoid:** Whitelist column names (Pattern 3), bind values with `?`.

### Pitfall 6: gspread tab missing / quota at startup blocks boot
**What goes wrong:** `sh.worksheet("Отобранные")` raises `WorksheetNotFound` if the tab isn't created yet; a startup refresh that doesn't catch it could crash boot or leave gating in an undefined state.
**How to avoid:** Wrap refresh in try/except (mirror `sheets.py` fail-soft). With `preselect_enabled` default `off`, a missing tab must NOT break `/start`. Log and leave the allowlist empty; if gating is on and the list is empty, fail-open or show a clear admin warning (Open Question 2).

## Code Examples

### Adding the schema (db.py init_db, additive)
```python
# inside init_db(), alongside existing CREATE TABLE blocks
await db.execute('''
    CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT,
        photo_file_id TEXT,
        filter_spec TEXT,           -- JSON blob of [{field,op,value}] (discretion: JSON vs columns)
        scheduled_at TEXT NOT NULL, -- "%Y-%m-%d %H:%M:%S"
        status TEXT DEFAULT 'pending',  -- pending | sent | cancelled
        created_by INTEGER,
        created_at TEXT
    )
''')
await _ensure_column(db, "reg_started", "nudged_at", "TEXT")   # D-15
```

### Nudge scan helper (SCHED-03)
```python
# database/db.py — older than threshold AND not yet nudged (one-shot via nudged_at)
async def get_nudge_candidates(older_than_minutes: int) -> list[int]:
    cutoff = (datetime.now() - timedelta(minutes=older_than_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM reg_started WHERE started_at < ? AND nudged_at IS NULL",
            (cutoff,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

async def mark_nudged(telegram_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE reg_started SET nudged_at = ? WHERE telegram_id = ?",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), telegram_id))
        await db.commit()
```
> Note `started_at` is stored as `"%Y-%m-%d %H:%M:%S"` (verified `db.py:404`), so the lexicographic `<` cutoff comparison is correct.

### VERIF gate insertion (registration.py cmd_start, after subscription check ~line 541)
```python
# conditional on setting (D-13, default off so live flow is unchanged)
if (await get_setting("preselect_enabled") or "off") == "on":
    uname = message.from_user.username
    manual_ids = _parse_manual_ids(await get_setting("preselect_manual_ids"))
    if uname is None and user_id not in manual_ids:
        await message.answer(await get_setting("preselect_no_username_text")
                             or "Установи @username в настройках Telegram, затем нажми /start.")
        return
    if uname is not None and not is_allowed(uname) and user_id not in manual_ids:
        link = await get_setting("preselect_link") or ""
        await message.answer((await get_setting("preselect_fail_text")
                             or "Отбор не пройден.") + (f"\n{link}" if link else ""))
        return
# ... existing flow continues
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| APScheduler 3.x `add_job`/`AsyncIOScheduler`/jobstores | APScheduler 4.0 `add_schedule`/`AsyncScheduler`/datastores | 4.0 still **alpha** (4.0.0a6) as of 2026-06 | We stay on 3.x (stable, CLAUDE.md lock). Docs default to 4.0 — must filter (Pitfall 1). |
| `aiogram` 2.x `RetryAfter` | `aiogram` 3.x `TelegramRetryAfter` (in `aiogram.exceptions`) | aiogram 3.0 | Use `TelegramRetryAfter`, attr `.retry_after`. Confirmed in installed 3.24. |

**Deprecated/outdated for this project:**
- Any APScheduler 4.0 example — not installed, API-incompatible.
- `BackgroundScheduler`/`MemoryJobStore` — forbidden by CLAUDE.md.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (existing `tests/` dir, 8 phase-suffixed files) |
| Config file | none detected (no pytest.ini/pyproject test config) |
| Async support | **pytest-asyncio is installed but version-broken** (`ImportError: cannot import name 'FixtureDef'`). Existing tests are deliberately **pure synchronous helper tests** — follow that convention. `[VERIFIED: import error + test_reminders_phase2.py]` |
| Quick run command | `python -m pytest tests/test_<module>_phase3.py -x` |
| Full suite command | `python -m pytest tests/ -q` |

### Established test convention (copy it)
Phase 2 (`test_reminders_phase2.py`) tests pure helper functions (`_reminder_enabled`, `_reminder_interval`) with no async, no DB, no Telegram. **Design Phase 3 the same way:** extract pure, side-effect-free helpers and unit-test those. Do NOT attempt async/DB/gspread integration tests (the async plugin is broken and the bot has no test DB harness).

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| COMM-03 | AND `WHERE` builder produces correct clause/params from spec; rejects non-whitelisted fields | unit (pure) | `python -m pytest tests/test_filters_phase3.py -x` | ❌ Wave 0 |
| COMM-02 | date after/before → correct `>=`/`<` op + value passthrough | unit (pure) | same file | ❌ Wave 0 |
| COMM-04 | 429-retry decision: retry-success not counted blocked; genuine error counted | unit (pure helper extracted from loop) | `tests/test_broadcast_429_phase3.py` | ❌ Wave 0 |
| VERIF-01/10 | `_normalize` (strip/@/lower) + `is_allowed` set membership | unit (pure) | `tests/test_allowlist_phase3.py` | ❌ Wave 0 |
| VERIF-02 | `_parse_manual_ids` CSV → set[int]; usernameless branch logic | unit (pure) | same file | ❌ Wave 0 |
| SCHED-03 | nudge-candidate predicate: cutoff math + `nudged_at IS NULL` exclusion (test the pure cutoff/format helper) | unit (pure) | `tests/test_nudge_phase3.py` | ❌ Wave 0 |
| SCHED-01 | settings parsing for thresholds/intervals; datetime-format helper | unit (pure) | `tests/test_scheduler_helpers_phase3.py` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `python -m pytest tests/test_<touched>_phase3.py -x`
- **Per wave merge:** `python -m pytest tests/ -q`
- **Phase gate:** full suite green before `/gsd-verify-work`. Plus one **manual** end-to-end check that cannot be unit-tested: schedule a broadcast 2 min out, restart the bot, confirm it fires (SCHED-01 success-criterion #1) — flag as manual.

### Wave 0 Gaps
- [ ] `tests/test_filters_phase3.py` — COMM-02/03 query-builder (pure)
- [ ] `tests/test_broadcast_429_phase3.py` — COMM-04 retry/block accounting (extract pure classifier)
- [ ] `tests/test_allowlist_phase3.py` — VERIF normalize + manual-id parse
- [ ] `tests/test_nudge_phase3.py` — SCHED-03 cutoff helper
- [ ] `tests/test_scheduler_helpers_phase3.py` — settings/threshold parsing
- [ ] No async test harness exists and pytest-asyncio is broken — **do not add one**; keep helpers pure so they're testable without it.

## Security Domain

> `security_enforcement` not set in config (absent = enabled). This is an admin-tool / internal bot; the relevant surface is input validation and authorization on new entry points.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V4 Access Control | yes | Every new callback handler must re-check `callback.from_user.id in config.ADMIN_IDS` — message-level `is_admin` filter does NOT cover callbacks (precedent: `_start_segment_broadcast` re-checks, admin.py:872). |
| V5 Input Validation | yes | Filter-field whitelist + parameterized SQL (Pattern 3); manual-id CSV parsed with int() guard; `html.escape` any user/sheet text echoed back. |
| V6 Cryptography | no | No new crypto; reuses existing token/credentials handling. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via filter value | Tampering | Whitelist columns, bind `?` params (Pattern 3) |
| Unauthorized broadcast/schedule via callback | Elevation of Privilege | Per-callback `ADMIN_IDS` re-check |
| Allowlist tab spoofing / stale cache admits wrong users | Spoofing | Single source-of-truth tab, manager-controlled; admin manual-refresh; fail-closed option for empty list (Open Q2) |
| Job pickle from untrusted source | Tampering | jobstore is local file only; not network-exposed — low risk |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Module-level bot reference + module-level job functions is the idiomatic 3.x way to inject a non-serializable client into persisted jobs | Pattern 1 | If wrong, jobs can't access `bot`; mitigations: pass nothing but `broadcast_id`, read bot from module global — low risk, widely used. |
| A2 | `misfire_grace_time=3600` + `coalesce=True` are the right defaults for "broadcast scheduled during a brief outage should still fire" | Pattern 1 / Pitfall 4 | Product decision — see Open Q1. Wrong value → either drops or late-fires a broadcast. |
| A3 | Default allowlist tab name "Отобранные" and default thresholds (120 min inactivity, 15 min scan, refresh interval) | bot_settings keys | All are CONTEXT-designated Claude's discretion; user may want different numbers — confirm at plan/discuss. |
| A4 | gspread `worksheet(name)` reads a tab by title within the existing spreadsheet (service account already has access) | Pattern 4 | If the service account lacks access to the tab, refresh fails; covered by fail-soft (Pitfall 6). |
| A5 | aiogram 3.24.0 (not 3.29) is acceptable for this phase; no upgrade needed | Stack note | Low — `TelegramRetryAfter`/`send_copy` exist in 3.24 (verified). |

## Open Questions (RESOLVED)

1. **Misfire policy for past-due scheduled broadcasts.** RESOLVED → `misfire_grace_time=3600` + `coalesce=True` (fire if within 1h of `run_date`; coalesce duplicate catch-ups into one). Longer outages surface as pending past-due broadcasts in admin UI. Encoded in Plan 03-01.
2. **Fail-open vs fail-closed when `preselect_enabled=on` but the allowlist is empty** (tab missing / refresh failed). RESOLVED (owner sign-off 2026-06-27) → **FAIL-OPEN + loud admin alert**: admit everyone and alert `ADMIN_IDS` when gating is ON and `allowlist_size()==0`. Protects live registration from a config typo locking out all delegates; accepted risk dispositioned in Plan 03-05 threat_model (T-3-11). Encoded in Plan 03-05.
3. **Manual telegram_id allowlist storage (D-12 discretion):** RESOLVED → `bot_settings` CSV string (`preselect_manual_ids`); no new table, edited via existing settings command. Encoded in Plan 03-05.
4. **Filter-spec serialization (D-05 discretion):** RESOLVED → JSON blob in `scheduled_broadcasts.filter_spec` (list of `{field, op, value}`); whitelisted columns + `?` binds at query time. Encoded in Plan 03-04.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| apscheduler | SCHED-01/03 | ✓ | 3.11.2 | — |
| sqlalchemy | jobstore backend | ✓ | 2.0.49 | — |
| aiogram | all | ✓ | 3.24.0 | — |
| aiosqlite | app DB | ✓ | installed | — |
| gspread + google-auth | VERIF allowlist | ✓ | installed | If Sheet/creds unset, gating must fail-soft (matches `sheets.py` guard) |
| Google Sheet "Отобранные" tab | VERIF-01 | ✗ (must be created by DXP manager) | — | `preselect_enabled=off` default → no impact until created |
| Writable `data/` for `jobs.sqlite` | SCHED-01 | ✓ (holds forum.db) | — | — |
| pytest-asyncio | (not used) | ⚠ broken | mismatch | Keep tests pure-sync (existing convention) |

**Missing dependencies with no fallback:** none blocking — all code dependencies installed.
**Missing with fallback:** the Google Sheet allowlist tab (manager-created; gating off by default until then).

## Sources

### Primary (HIGH confidence)
- Installed environment probes (2026-06-27): `pip show APScheduler` → 3.11.2; `python -c import` of `AsyncIOScheduler`, `SQLAlchemyJobStore`, `sqlalchemy 2.0.49`, `aiogram 3.24.0`; `inspect.signature(AsyncIOScheduler.add_job)` — all `[VERIFIED]`.
- Existing source (source of truth, read this session): `handlers/admin.py` (broadcast menu/loop/`_welcome_flipped` 429 pattern), `services/reminders.py` (startup task), `services/sheets.py` (`to_thread` gspread), `database/db.py` (`_ensure_column`, `bot_settings`, `reg_started` helpers, ISO date format), `handlers/registration.py` (`cmd_start` gate point, `registration_date` format), `main.py` (startup wiring), `handlers/states.py`, `config.py`.
- CLAUDE.md tech-stack lock section (APScheduler 3.11.2 / `AsyncIOScheduler` / `SQLAlchemyJobStore`; what NOT to use) — already Context7-sourced from `/agronholm/apscheduler` 3.x and `/websites/aiogram_dev_en_v3_27_0`.

### Secondary (MEDIUM confidence)
- APScheduler 3.x user guide (`apscheduler.readthedocs.io/en/3.x/userguide.html`) — `AsyncIOScheduler` + `SQLAlchemyJobStore`, `add_job`, `misfire_grace_time`, `coalesce`, `replace_existing` `[CITED]`.

### Tertiary / cautionary (LOW confidence — do NOT use)
- Context7 `/agronholm/apscheduler` default fetch returns the **4.0** API (`AsyncScheduler`/`add_schedule`/`SQLAlchemyDataStore`) — incompatible with the locked 3.x stack. Flagged as Pitfall 1, not a source to copy.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every version probed in the live environment; `add_job` signature introspected.
- Architecture / integration points: HIGH — all insertion points read from current source with line numbers.
- APScheduler injection pattern (Pattern 1): MEDIUM — idiomatic and verified-importable, but the bot-injection specifics are `[ASSUMED]` (A1); no in-repo precedent yet since this is the first scheduler.
- Pitfalls: HIGH for 1 (verified via ctx7 fetch) and 2 (well-known SQLite behavior); MEDIUM for misfire tuning.

**Research date:** 2026-06-27
**Valid until:** ~2026-07-27 (stable stack; only risk is APScheduler 4.0 reaching stable, which would not change our 3.x lock).
