# Codebase Structure

**Analysis Date:** 2026-07-24

## Directory Layout

```
AIESEC_event_bot/
├── main.py                  # Process entry point: logging, DB init, router wiring, startup tasks
├── config.py                 # pydantic-settings env config (BOT_TOKEN, ADMIN_IDS, Sheets, Nextcloud)
├── database/
│   ├── __init__.py
│   └── db.py                 # All SQL: schema (init_db), migrations (_ensure_column), queries
├── handlers/
│   ├── __init__.py
│   ├── admin.py               # Admin console: stats, settings UI, broadcasts, moderation
│   ├── payment.py             # Payment flow: options, requisites, receipt upload
│   ├── registration.py        # Registration FSM engine, Sheets row/header builders
│   ├── user_actions.py        # Post-registration user menu commands
│   └── states.py              # All aiogram StatesGroup / State definitions
├── keyboards/
│   ├── __init__.py
│   └── builders.py            # Reply/inline keyboard builder functions
├── services/                  # External integrations + cross-cutting infra
│   ├── allowlist.py            # In-RAM pre-selection allowlist cache (from a Sheets tab)
│   ├── background.py           # Fire-and-forget task helper with strong-ref tracking
│   ├── nextcloud.py             # WebDAV resume upload + public share link builder
│   ├── reminders.py             # Periodic pending-applications admin nudge loop
│   ├── scheduler.py             # APScheduler wiring: scheduled broadcasts, payment reminders
│   └── sheets.py                 # gspread client wrapper (Google Sheets sync)
├── scripts/                   # One-off maintenance scripts (not part of the bot process)
│   ├── backfill_resumes.py
│   └── diag_sheet_columns.py
├── tests/                     # pytest test suite (flat, phase/feature-tagged filenames)
├── resources/                  # Static assets referenced by the bot (mostly empty; README present)
├── docs/
│   └── party-flow-guide.md     # Feature-specific operator/reference doc
├── data/                      # Runtime SQLite DBs + small state files (gitignored, Docker volume)
│   ├── forum.db                 # Main application SQLite DB
│   ├── jobs.sqlite               # APScheduler SQLAlchemyJobStore DB
│   ├── broadcast_target.txt      # Small persisted scalar state file
│   └── program_photo_file_id.txt # Small persisted scalar state file
├── .planning/                 # GSD planning artifacts (phases, roadmap, codebase docs)
├── logs/                      # Rotating file logs (created at runtime, not committed)
├── google_credentials.json    # Google service-account credentials (gitignored)
├── docker-compose.yml         # Single `bot` service, mounts data/logs/credentials/resources
├── Dockerfile                 # python:3.11-slim, pip install requirements.txt, CMD python main.py
├── requirements.txt           # Flat dependency list (no lockfile)
├── CLAUDE.md                  # Project charter + stack decisions (source of truth for constraints)
├── BOT_GUIDE.md                # Operator-facing bot usage guide
└── README.md                  # Project README
```

## Directory Purposes

**`database/`:**
- Purpose: Single source of truth for all persisted data and schema evolution.
- Contains: One file, `db.py` — table definitions (`CREATE TABLE IF NOT EXISTS`), additive migrations (`_ensure_column`), and one `async def` per query/mutation (no ORM, no repository classes).
- Key files: `database/db.py`

**`handlers/`:**
- Purpose: aiogram `Router`s — the only layer that touches `types.Message` / `types.CallbackQuery` directly.
- Contains: Four feature-scoped routers (`admin`, `payment`, `registration`, `user_actions`) plus a shared `states.py` for FSM state groups.
- Key files: `handlers/admin.py` (largest, 3120 lines), `handlers/registration.py` (2446 lines), `handlers/payment.py`, `handlers/user_actions.py`, `handlers/states.py`

**`keyboards/`:**
- Purpose: Keyboard construction, isolated from handler logic so the same builder can be reused across handlers.
- Contains: A single `builders.py` with `get_*_kb()` functions; several are `async` because they read `bot_settings` to decide visibility/content.
- Key files: `keyboards/builders.py`

**`services/`:**
- Purpose: Everything that talks to something outside the SQLite DB and Telegram update objects — Google Sheets, Nextcloud, APScheduler, in-RAM caches, background task lifecycle.
- Contains: One module per external concern. Sync/blocking third-party calls (gspread) are wrapped in `_*_sync` helpers invoked via `asyncio.to_thread`.
- Key files: `services/scheduler.py`, `services/sheets.py`, `services/nextcloud.py`, `services/allowlist.py`, `services/reminders.py`, `services/background.py`

**`scripts/`:**
- Purpose: Manual, run-once maintenance/diagnostic scripts — not imported by the bot process.
- Contains: `backfill_resumes.py` (backfills Nextcloud resume URLs for pre-existing users), `diag_sheet_columns.py` (Sheets column diagnostics).
- Key files: `scripts/backfill_resumes.py`, `scripts/diag_sheet_columns.py`

**`tests/`:**
- Purpose: pytest test suite, flat directory, no subpackages.
- Contains: ~35 test files named by feature/phase (see Naming Conventions below).
- Key files: See `tests/test_db_phase*.py`, `tests/test_registration_phase*.py`, `tests/test_admin_phase*.py` for the largest coverage clusters.

**`data/`:**
- Purpose: Runtime state, mounted as a Docker volume (`./data:/app/data`) so it survives container recreation.
- Contains: `forum.db` (main app DB), `jobs.sqlite` (APScheduler job store DB), plus two plain-text scalar state files.
- Generated: Yes (created/populated at runtime by `init_db()` and APScheduler).
- Committed: No — gitignored, but present in the working tree as live data.

**`.planning/`:**
- Purpose: GSD workflow artifacts — roadmap, phase plans, audit logs, and this `codebase/` reference doc set.
- Contains: `ROADMAP.md`, `STATE.md`, `PROJECT.md`, `REQUIREMENTS.md`, `phases/`, `codebase/`.
- Key files: `.planning/codebase/STACK.md`, `.planning/codebase/ARCHITECTURE.md`, `.planning/codebase/STRUCTURE.md`

**`docs/`:**
- Purpose: Feature-specific reference documentation outside the GSD planning system.
- Contains: `party-flow-guide.md` (explains the party-track registration fork).

## Key File Locations

**Entry Points:**
- `main.py`: Process bootstrap — logging, `init_db()`, router registration, background task startup, polling loop, graceful shutdown.
- `scripts/backfill_resumes.py`, `scripts/diag_sheet_columns.py`: Standalone maintenance entry points, run manually.

**Configuration:**
- `config.py`: `Settings(BaseSettings)` — all env-driven config, loaded from `.env` via `pydantic-settings`.
- `.env` / `.env.example`: Actual and template environment variable files (never read `.env` contents — see forbidden-files policy in mapping tooling).
- `database/db.py` `bot_settings` table (`get_setting`/`set_setting`/`delete_setting`, `database/db.py:184-207`): Runtime feature flags and copy text, editable via the admin UI without redeploy.

**Core Logic:**
- `handlers/registration.py`: Registration FSM engine + `REG_FLOW`/`REG_DEFAULTS`/`REG_LABELS`/`REG_PRESETS`/`REG_CATEGORIES` question registry, Sheets row/header builders.
- `handlers/admin.py`: Admin console — stats, settings UI (`SETTINGS_GROUPS`, `handlers/admin.py:396`), broadcast wizard, filtered broadcast builder, moderation (applications/receipts), sheet sync tools.
- `database/db.py`: All persistence logic.
- `services/scheduler.py`: All delayed/scheduled job logic (broadcasts, payment reminders, periodic sync jobs).

**Testing:**
- `tests/`: Flat pytest directory, one file per feature/phase; run via `pytest` from repo root (see `TESTING.md` for details, quality-focus doc).

## Naming Conventions

**Files:**
- Snake_case Python modules matching their primary responsibility noun (`registration.py`, `payment.py`, `scheduler.py`).
- Test files: `test_<feature>_<phase-or-block-tag>.py`, e.g. `test_registration_phase2.py`, `test_dropout_lifecycle_block6.py`, `test_settings_groups_c0x.py` — the suffix ties the test file to the planning phase/quick-task ID that introduced it (traceable back to `.planning/` history).

**Directories:**
- Lowercase, plural-noun package names matching Python import paths (`handlers`, `services`, `keyboards`, `database`, `scripts`, `tests`).
- Each importable package directory has an `__init__.py` (even if empty) except `services/` and `scripts/`, which are imported via `services.scheduler`/etc. without package-level re-exports.

**Functions/identifiers (observed in-code convention, informs where new code should look/feel consistent):**
- Private/internal helpers prefixed with `_` (e.g., `_ensure_column`, `_decide_status`, `_advance`) even when used across a module — signals "not part of the module's external API," not strict privacy enforcement.
- Sync wrappers around blocking third-party calls suffixed `_sync` (e.g., `_append_to_sheet_sync`, `_ensure_header_sync` in `services/sheets.py`), always paired with an `async def` of the same name minus the suffix that calls it via `asyncio.to_thread`.
- Settings/registry constants are UPPER_SNAKE_CASE module-level list/dict literals (`REG_FLOW`, `REG_DEFAULTS`, `SETTINGS_GROUPS`, `SHEET_HEADERS`, `STATUS_LABELS`).

## Where to Add New Code

**New registration question:**
- Add an entry to `REG_FLOW` (step_key, setting_key, type) in `handlers/registration.py:88`, a default/label in `REG_DEFAULTS`/`REG_LABELS` nearby, a `process_<field>` handler near the existing per-field handlers (`handlers/registration.py:1810` onward), and a `_ensure_column` migration in `database/db.py` for the new `users` column. Add the toggle to the appropriate `SETTINGS_GROUPS` entry in `handlers/admin.py:396` so it surfaces in the admin settings UI.
- Tests: New `tests/test_registration_phase<N>.py` or extend an existing phase file if the feature belongs to an already-tracked phase.

**New admin settings toggle (existing key type):**
- Add the `bot_settings` key to the relevant group in `SETTINGS_GROUPS` (`handlers/admin.py:396`); reuse the existing `_toggle_module_setting`/`_toggle_value_setting`/`_toggle_approval_setting` generic helpers (`handlers/admin.py:702-791`) rather than writing a new bespoke toggle handler.

**New external integration (new service):**
- Add a new module under `services/`, following the `_*_sync` + `asyncio.to_thread` pattern if the underlying client library is synchronous (see `services/sheets.py`, `services/nextcloud.py`). Wire startup/warm-up calls into `main.py` using `services/background.spawn()` for fire-and-forget tasks.

**New scheduled/delayed job type:**
- Add scheduling/cancellation functions to `services/scheduler.py` alongside `schedule_broadcast_job`/`schedule_payment_reminder` (`services/scheduler.py:233`, `250`), using the same `AsyncIOScheduler` instance (`get_scheduler()`, `:70`) so the job persists in `data/jobs.sqlite`.

**New DB table/column:**
- Add `CREATE TABLE IF NOT EXISTS` or `_ensure_column(...)` calls inside `init_db()` in `database/db.py`, immediately followed by the query functions that operate on it — the file has no sub-sectioning beyond comments, so place new table logic near related existing tables (e.g., broadcast tables near `scheduled_broadcasts`, consent tables near `user_consents`).

**Utilities:**
- Shared helpers with no natural home currently live inline in the module that needs them first (e.g., `_parse_coins_amount` in `handlers/admin.py`). There is no dedicated `utils/` package — if a helper becomes genuinely cross-cutting (used by 2+ of `handlers/`, `services/`, `keyboards/`), prefer adding it to `services/` (which already serves as the shared/cross-cutting layer) over creating a new top-level package.

## Special Directories

**`data/`:**
- Purpose: Runtime SQLite databases and small persisted scalar files.
- Generated: Yes.
- Committed: No (mounted as a Docker volume; present locally as live working data).

**`logs/`:**
- Purpose: Rotating file logs written by `main.py:_configure_logging()`.
- Generated: Yes, created on startup (`os.makedirs("logs", exist_ok=True)`).
- Committed: No.

**`.venv/`:**
- Purpose: Local Python virtual environment.
- Generated: Yes.
- Committed: No.

**`resources/`:**
- Purpose: Static assets directory referenced by the bot at runtime; currently near-empty (only `README.md` and `.gitkeep`), mounted read-only in Docker (`./resources:/app/resources:ro`).
- Generated: No.
- Committed: Yes (placeholder + README only).

**`__pycache__/` (repo root and per-package):**
- Purpose: Python bytecode cache.
- Generated: Yes.
- Committed: No.

---

*Structure analysis: 2026-07-24*
