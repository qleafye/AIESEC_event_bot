<!-- refreshed: 2026-07-24 -->
# Architecture

**Analysis Date:** 2026-07-24

## System Overview

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot API (long polling)              │
└───────────────────────────────────┬────────────────────────────────┘
                                     │ Update
                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    aiogram Dispatcher  `main.py`                     │
│   Routers included in fixed order (first match wins):                │
│   admin.router → payment.router → registration.router → user_actions.router │
└───────┬───────────────┬───────────────┬────────────────┬────────────┘
        │               │               │                │
        ▼               ▼               ▼                ▼
┌───────────────┐ ┌─────────────┐ ┌──────────────────┐ ┌──────────────┐
│ handlers/admin │ │handlers/    │ │handlers/          │ │handlers/     │
│ .py            │ │payment.py   │ │registration.py    │ │user_actions.py│
│ (admin console,│ │(payment flow│ │(FSM-driven reg    │ │(post-reg menu│
│ settings,      │ │ + receipts) │ │ flow engine,       │ │ commands)    │
│ broadcasts,    │ │             │ │ Sheets row builder)│ │              │
│ moderation)    │ │             │ │                    │ │              │
└───────┬────────┘ └──────┬──────┘ └─────────┬──────────┘ └──────┬───────┘
        │                 │                  │                   │
        └────────┬────────┴──────────┬───────┴───────────────────┘
                  ▼                   ▼
         ┌─────────────────┐  ┌──────────────────────────┐
         │ keyboards/       │  │ services/ (integration    │
         │ builders.py      │  │  layer)                   │
         │ (reply/inline kb,│  │  scheduler.py, sheets.py,  │
         │  reads settings) │  │  nextcloud.py, allowlist.py│
         └────────┬─────────┘  │  reminders.py, background.py│
                  │            └──────────────┬────────────┘
                  ▼                            ▼
         ┌───────────────────────────────────────────────────┐
         │             database/db.py (data access layer)     │
         │  aiosqlite raw SQL, `_ensure_column` migrations,    │
         │  one function per query, no ORM                    │
         └───────────────────────┬─────────────────────────────┘
                                  ▼
                     ┌────────────────────────┐
                     │  data/forum.db (SQLite) │
                     │  data/jobs.sqlite       │
                     │  (APScheduler job store)│
                     └────────────────────────┘

External:
  Google Sheets (gspread)   ← services/sheets.py
  Nextcloud WebDAV          ← services/nextcloud.py
  Telegram Bot API          ← aiogram Bot instance passed through handlers
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Entry point / wiring | Logging setup, DB init, router registration, scheduler/allowlist/reminder task startup, graceful shutdown | `main.py` |
| Config | Env-driven settings (pydantic-settings), secrets (`SecretStr`) | `config.py` |
| Registration flow engine | Configurable step-by-step FSM registration (`REG_FLOW`), Sheets row/header builders, party-track fork logic | `handlers/registration.py` |
| Admin console | Stats, settings UI (grouped), broadcasts (immediate/scheduled/filtered), moderation queue (applications, receipts), sheet sync tools | `handlers/admin.py` |
| Payment flow | Payment option selection, requisites, receipt upload, deadline reminders | `handlers/payment.py` |
| Post-registration user commands | Main menu actions: coins, leaderboard, referral link, info/program/speakers, ask-organizer | `handlers/user_actions.py` |
| FSM state groups | All `StatesGroup` definitions shared across handlers | `handlers/states.py` |
| Keyboard builders | Reply/inline keyboard construction, reads `bot_settings` to decide which buttons show | `keyboards/builders.py` |
| Data access layer | All SQL (raw aiosqlite), schema migrations via `_ensure_column`, one function per query/mutation | `database/db.py` |
| Scheduler service | APScheduler (`AsyncIOScheduler` + `SQLAlchemyJobStore`) — scheduled broadcasts, payment reminders, sheet sync jobs, allowlist refresh job | `services/scheduler.py` |
| Google Sheets service | gspread client wrapper; sync functions run in thread executor (`asyncio.to_thread`) since gspread is sync | `services/sheets.py` |
| Nextcloud service | WebDAV upload of resumes, builds public share links | `services/nextcloud.py` |
| Allowlist service | In-RAM cache of pre-selected usernames, refreshed from a Sheets tab, avoids per-`/start` Sheets call | `services/allowlist.py` |
| Reminder loop | Periodic "pending applications" nudge to admins | `services/reminders.py` |
| Background task helper | Fire-and-forget `asyncio.create_task` wrapper that holds strong refs (prevents GC-drop of suspended tasks) | `services/background.py` |

## Pattern Overview

**Overall:** Layered monolith — single aiogram process, handlers → services → database layers, no framework-level DI, no ORM. Configuration-driven behavior: almost every feature toggle and question is stored as a row in the `bot_settings` key/value table and read at request time via `get_setting()`.

**Key Characteristics:**
- Single Python process, `asyncio.run(main())`, long polling (no webhook server, no ASGI app).
- Handlers are aiogram `Router` instances registered in a fixed, meaningful order in `main.py` (admin first so admin commands aren't shadowed by registration filters).
- Feature flags and copy text live in the `bot_settings` table, not in code/config files — this lets admins reconfigure registration questions, approval modes, and broadcast copy without a redeploy.
- Registration flow is data-driven: `REG_FLOW` (list of `(step_key, setting_key, type)` tuples) in `handlers/registration.py` drives which questions are asked, in what order, gated per-step by a `bot_settings` toggle (`_is_step_enabled`). Admin toggles reorder/enable/disable steps by editing settings, not code.
- Sync third-party libraries (`gspread`) are wrapped with `asyncio.to_thread` in `services/sheets.py` so blocking HTTP calls don't block the event loop.
- Scheduled/delayed work (broadcasts, payment reminders) persists through restarts via APScheduler's `SQLAlchemyJobStore` backed by `data/jobs.sqlite` — required because aiogram's `MemoryStorage` FSM state does not survive a restart.

## Layers

**Handler layer (`handlers/`):**
- Purpose: Parse Telegram updates (messages/callbacks), drive FSM state transitions, format outgoing text/keyboards.
- Location: `handlers/`
- Contains: `Router` instances, `@router.message()` / `@router.callback_query()` handlers, FSM step logic.
- Depends on: `database/db.py` (reads/writes), `services/*` (Sheets, scheduler, Nextcloud), `keyboards/builders.py`, `handlers/states.py`.
- Used by: `main.py` (registers routers with the `Dispatcher`).

**Keyboard layer (`keyboards/`):**
- Purpose: Build `ReplyKeyboardMarkup` / `InlineKeyboardMarkup` objects, deciding button visibility from `bot_settings`.
- Location: `keyboards/builders.py`
- Contains: Pure builder functions (mostly `async def get_*_kb()`), no handler logic.
- Depends on: `database/db.py` (`get_setting`), lazy-imports `handlers/payment.py` (to avoid circular import).
- Used by: `handlers/*.py`.

**Service layer (`services/`):**
- Purpose: Integration with external systems and cross-cutting infrastructure (scheduling, Sheets, Nextcloud, background tasks).
- Location: `services/`
- Contains: One module per external concern; sync/blocking work isolated in `_*_sync` helper functions run via `asyncio.to_thread`.
- Depends on: `database/db.py` (settings, broadcast/job persistence), `config.py`.
- Used by: `handlers/*.py`, `main.py`.

**Data access layer (`database/`):**
- Purpose: All SQL. Owns schema (`init_db`), migrations (`_ensure_column`), and one function per query/mutation.
- Location: `database/db.py`
- Contains: `aiosqlite` connections opened per-call (`async with aiosqlite.connect(...)`), no connection pool, no ORM.
- Depends on: `config.py` (`DB_PATH`).
- Used by: `handlers/*.py`, `services/*.py`.

## Data Flow

### Primary Request Path — user registration

1. User sends `/start` (with optional referral payload) → `cmd_start()` (`handlers/registration.py:1347`).
2. Allowlist / subscription checks (`services/allowlist.py`, `is_subscribed()` via `getChatMember`).
3. `_start_registration_flow()` sets FSM state to `Registration.full_name` and sends the first prompt (`handlers/registration.py:1202`).
4. Each subsequent user reply is handled by a `process_*` handler filtered on the current `State`, calling `_store_text`/`_store_choice` then `_advance()` (`handlers/registration.py:767`) which looks up the next enabled step from `REG_FLOW`/party track and re-prompts via `_ask_step()` (`handlers/registration.py:533`).
5. On the final step, `_decide_status()` (`handlers/registration.py:65`) resolves `pending` vs `approved` from `bot_settings` (`full_approval`/`short_approval`/`party_approval`).
6. `add_user()` (`database/db.py:208`) inserts the row into SQLite; `append_to_sheet()` / `append_to_named_sheet()` (`services/sheets.py`) mirror the row to Google Sheets in a background task (`services/background.spawn`).
7. If payment is enabled, flow hands off to `handlers/payment.py` (`start_payment_step()`); otherwise the confirmation/approve text is sent directly.

### Admin Moderation Flow

1. Admin opens `/admin` → `admin.router` intercepts (registered first in `main.py`) → `build_admin_keyboard()` (`handlers/admin.py:97`).
2. "Заявки" (applications) → `get_pending_users(limit, offset)` (`database/db.py:709`) — paginated (never one Telegram message per applicant, per the moderation-at-scale constraint).
3. Approve/Reject → `approve_user_atomic()` / `reject_user()` (`database/db.py:686`, `698`) update `users.status`; sheet status label synced via `update_status_in_sheet()` (`services/sheets.py`).
4. Broadcasts (immediate/scheduled/filtered) build a target-id list via `count_and_list_filtered()` (`database/db.py:899`) then either send directly or persist via `create_scheduled_broadcast()` + `schedule_broadcast_job()` (`services/scheduler.py:233`) so the send survives a restart.

**State Management:**
- Conversation state (registration/broadcast wizard steps) lives in aiogram's `MemoryStorage` FSM — lost on restart by design; recoverable via `reg_started` table (`get_reg_started_track`) for reg-in-progress tracking that must survive restart.
- Scheduled/delayed jobs (broadcasts, payment reminders) live in `data/jobs.sqlite` via APScheduler's `SQLAlchemyJobStore` — survives restart, reconciled at startup (`reconcile_scheduled_broadcasts()`, `services/scheduler.py:138`).
- Feature flags / copy text live in `bot_settings` (SQLite key/value table), read per-request via `get_setting()` — no in-process cache except the allowlist (`services/allowlist.py` module-global `_allowlist` set).

## Key Abstractions

**`REG_FLOW` / `REG_DEFAULTS` / `REG_LABELS` / `REG_PRESETS` / `REG_CATEGORIES`:**
- Purpose: Data-driven registration question registry — each entry is `(step_key, setting_key, type)`. Determines ask order, per-step enable/disable, question text, and event-type presets (YouLead forum vs RusCo conference).
- Examples: `handlers/registration.py:88` (`REG_FLOW`), `:197` (`REG_DEFAULTS`), `:243` (`REG_LABELS`), `:295` (`REG_PRESETS`), `:364` (`REG_CATEGORIES`).
- Pattern: Module-level list/dict constants imported directly by `handlers/admin.py` (no separate registry module) to render the settings/questions admin UI and toggle steps.

**`SETTINGS_GROUPS`:**
- Purpose: Groups `bot_settings` keys into named UI sections (a grouping table, not a full per-key metadata registry) for the admin settings screen navigation.
- Examples: `handlers/admin.py:396`.
- Pattern: List of `(label, token, keys)` tuples; `_settings_group_keys()` (`handlers/admin.py:420`) resolves keys per group, with leftover-safety fallback for any `bot_settings` key not assigned to a group.

**`bot_settings` key/value store:**
- Purpose: Central feature-flag and copy-text store, replacing what would otherwise be hardcoded constants or a config file — enables no-redeploy reconfiguration between events (forum vs conference).
- Examples: `get_setting()`/`set_setting()` (`database/db.py:184`, `193`).
- Pattern: String keys, string values (`"on"`/`"off"` for toggles), read fresh on every use (no caching layer besides the allowlist).

**Append-only coins ledger:**
- Purpose: Gamification balance without mutable state — balance is `SUM(delta)`, never `UPDATE`.
- Examples: `add_coins()` (`database/db.py:498`), `get_balance()` (`:509`).
- Pattern: Insert-only audit-log table (`coins`), balance derived at read time.

**Fire-and-forget background task with strong ref (`services/background.spawn`):**
- Purpose: Prevent asyncio's weak-ref task GC from silently dropping suspended background jobs (Sheets export, album broadcast, reminder loop).
- Examples: `services/background.py:17`, used throughout `main.py` and handlers.
- Pattern: Module-level `set()` holds tasks until a `add_done_callback` discards them.

## Entry Points

**Bot process:**
- Location: `main.py`
- Triggers: `python main.py` (also the Docker `CMD`).
- Responsibilities: Configure logging, run `init_db()`, sync Sheets header, construct `Bot`/`Dispatcher`, register routers in fixed order, register a global `@dp.errors()` handler, start `pending_reminder_loop`, `init_scheduler`, `refresh_allowlist` as background tasks, then `dp.start_polling(bot)`. On shutdown: stop the APScheduler and close the bot session.

**`/start` command:**
- Location: `handlers/registration.py:1347` (`cmd_start`)
- Triggers: User sends `/start` (optionally with a referral deep-link payload).
- Responsibilities: Resolve referrer, check subscription/allowlist gates, resume or begin the registration FSM.

**`/admin` command:**
- Location: `handlers/admin.py` (router intercepts before other routers per `main.py` registration order)
- Triggers: Admin (id in `config.ADMIN_IDS`) sends `/admin`.
- Responsibilities: Render the admin console keyboard (stats, exports, applications, receipts, broadcast, settings, sheet sync tools).

**Standalone maintenance scripts:**
- Location: `scripts/backfill_resumes.py`, `scripts/diag_sheet_columns.py`
- Triggers: Run manually (`python scripts/...`), not part of the bot process.
- Responsibilities: One-off data migration (backfilling Nextcloud resume URLs) and Sheets column diagnostics.

## Architectural Constraints

- **Threading:** Single-threaded asyncio event loop. No worker threads except `asyncio.to_thread()` calls that isolate blocking `gspread` (Google Sheets) I/O in `services/sheets.py`. APScheduler's `SQLAlchemyJobStore` uses synchronous SQLAlchemy internally for job persistence, but the scheduler itself (`AsyncIOScheduler`) runs jobs as coroutines on the same event loop.
- **Global state:** `services/allowlist.py` holds a module-global `_allowlist: set[str]` cache, rebuilt wholesale (never mutated in place) by `refresh_allowlist()`. `services/background.py` holds a module-global `_background_tasks: set[asyncio.Task]` for strong-ref task tracking. `handlers/admin.py` holds a module-global `pending_albums = {}` dict for media-group broadcast staging.
- **Circular imports:** `keyboards/builders.py` lazy-imports `handlers/payment.py` inside a function body (not at module top) specifically to avoid a circular import (`payment` imports `get_main_menu_kb` from `builders`). `services/background.py` deliberately lives outside `main.py` so both startup code and handlers can import `spawn()` without creating a `handlers -> main` circular dependency. `services/allowlist.py` lazy-imports `services/sheets._get_allowlist_rows_sync` inside `refresh_allowlist()` for the same reason.
- **No connection pooling:** `database/db.py` opens a new `aiosqlite.connect()` per function call (`async with aiosqlite.connect(config.DB_PATH) as db:`), relying on SQLite's own locking; no shared connection pool.
- **FSM storage is not persistent:** `Dispatcher(storage=MemoryStorage())` in `main.py` — any in-progress FSM conversation (registration mid-flow, broadcast wizard) is lost on restart. Long-lived/critical state (registration-started tracking, scheduled broadcasts, payment reminders) is deliberately persisted to SQLite/APScheduler instead of relying on FSM.

## Anti-Patterns

### God-file handlers

**What happens:** `handlers/admin.py` (3120 lines) and `handlers/registration.py` (2446 lines) contain dozens of unrelated concerns each — stats rendering, settings CRUD, broadcast wizard, filter builder, receipt moderation, sheet sync all in one file for admin; FSM engine, Sheets row/header building, party-track logic, and every individual `process_*` step handler in one file for registration.
**Why it's wrong:** Any change (e.g., adding one settings toggle) touches a multi-thousand-line file, increasing merge-conflict risk and cognitive load per edit; it's hard to know where a given piece of logic lives without a full-text search.
**Do this instead:** When adding new admin functionality, group related handlers together near existing similarly-scoped code (e.g., all settings toggles are already clustered near `handlers/admin.py:686-860`; keep new toggles there) rather than appending at the end of the file. Do not attempt a full module split mid-feature — it is out of scope for incremental changes and risks router-order regressions.

### Settings validated only at read time

**What happens:** `bot_settings` values are free-form strings read via `get_setting()` with local parsing/defaulting scattered across call sites (e.g., `_reminder_interval()` in `services/reminders.py`, `_int_or_default()` in `services/scheduler.py`) rather than a single validated schema.
**Why it's wrong:** The same setting key can be parsed/defaulted differently in different files if a new call site is added carelessly, causing inconsistent behavior between, e.g., the admin UI's displayed value and the scheduler's interpreted value.
**Do this instead:** When reading a `bot_settings` key that already has a parser/default helper (grep for the key name first), reuse that helper instead of inlining new parsing logic.

## Error Handling

**Strategy:** Fail-soft by default for background/integration work (Sheets, Nextcloud, reminders); fail-loud only for the global aiogram error handler which logs and swallows (returns `True`) so a single handler exception never crashes the bot process.

**Patterns:**
- Global catch-all: `@dp.errors()` handler in `main.py:108` logs the exception and the offending update JSON, then returns `True` (marks handled) so `dp.start_polling` keeps running.
- Service-level fail-soft: `services/allowlist.refresh_allowlist()`, `services/sheets.py` calls, `services/nextcloud.py` uploads, and the `main._maybe_ensure_party_sheet_header()` startup hook all wrap external calls in `try/except Exception` and log a warning rather than raise, so a third-party outage never blocks bot startup or a user-facing flow.
- Handler-level: individual `process_*` handlers generally trust FSM state filters to guard invalid input paths, with explicit `process_*_invalid` fallback handlers (e.g., `process_resume_invalid`, `process_receipt_invalid`) for malformed input.

## Cross-Cutting Concerns

**Logging:** Standard library `logging`, configured once in `main.py:_configure_logging()`. Rotating file handler (`logs/bot.log`, 10MB x 5 backups) at the level from `config.LOG_LEVEL`; a separate stdout handler pinned to `WARNING+` keeps `docker logs` output clean. Third-party loggers (`aiogram.event`, `apscheduler`, `urllib3`, `gspread`, `asyncio`) are silenced to `WARNING`.

**Validation:** No schema/validation library beyond `pydantic-settings` for `config.py` env vars. User input validation is done inline per-handler (e.g., date parsing in `process_date_input`, phone contact parsing in `process_phone_contact`). SQL identifier safety for the migration helper (`_assert_identifier()` in `database/db.py:18`) guards against unsafe dynamic table/column names, though all current call sites use hardcoded literals.

**Authentication:** Admin-only actions gated by static Telegram user ID allowlist: `is_admin(message)` checks `message.from_user.id in config.ADMIN_IDS` (`handlers/admin.py:81`), sourced from the `.env`-configured `ADMIN_IDS` list. No role hierarchy — all admins have full access.

---

*Architecture analysis: 2026-07-24*
