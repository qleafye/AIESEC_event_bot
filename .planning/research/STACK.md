# Stack Research

**Domain:** Telegram event/registration bot (brownfield extension)
**Researched:** 2026-06-25
**Confidence:** HIGH

## Fixed Core Stack (Do Not Change)

| Technology | Version in use | Notes |
|------------|---------------|-------|
| aiogram | >=3.0.0 (latest stable: 3.29.0) | Async Telegram framework |
| aiosqlite | latest | Async SQLite driver |
| pydantic-settings | latest | `.env` config |
| gspread + google-auth | latest | Google Sheets sync |
| aiohttp-socks | latest | Proxy support |
| long polling | — | No webhooks, no change needed |

This section exists only to document what is frozen. Nothing below replaces or conflicts with these.

---

## New Libraries to Add

### Scheduler: APScheduler 3.x

**Recommendation:** `apscheduler==3.11.2` with `sqlalchemy>=2.0` for the SQLAlchemyJobStore.

**Why APScheduler 3.x, not 4.x:**
APScheduler 4.0 is pre-release (4.0.0a6, April 2025). The stable production branch is 3.x.
Latest stable: **3.11.2** (released December 22, 2025). Use this.

**Why SQLAlchemyJobStore, not MemoryJobStore:**
Current FSM uses `MemoryStorage` and is volatile — this is intentional for FSM but unacceptable for
scheduled jobs. Delayed broadcasts and payment reminders must survive bot restarts and Docker
container recreations. `SQLAlchemyJobStore` with `sqlite:///data/forum.db` persists jobs to the same
SQLite database the bot already uses (or an adjacent `data/scheduler.db`). Tables are created
automatically on first run.

**Why `AsyncIOScheduler`, not `BackgroundScheduler`:**
The bot runs a single asyncio event loop (long polling). `AsyncIOScheduler` attaches to that loop
and dispatches async job callbacks (coroutines) directly on it. `BackgroundScheduler` runs a separate
thread and cannot call aiogram Bot methods without `asyncio.run_coroutine_threadsafe`, which is
fragile. Use `AsyncIOScheduler`.

**Tradeoff to acknowledge (LOW severity):**
`SQLAlchemyJobStore` performs its I/O synchronously via SQLAlchemy, called from the asyncio event
loop thread. For a few dozen jobs max (broadcasts + payment reminders), SQLite operations take <1ms
and will not noticeably block the event loop. Acceptable for this scale.

**Why not a custom asyncio loop / `asyncio.sleep` scheduler:**
Requires building job serialization, persistence, and restart recovery from scratch.
The problem is solved — use the library.

**Why not aiojobs:**
aiojobs manages concurrent background tasks (fire-and-forget coroutines), not scheduled/delayed jobs.
No persistence. Not the right tool.

```python
# Integration pattern in main.py startup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

scheduler = AsyncIOScheduler(
    jobstores={
        "default": SQLAlchemyJobStore(url=f"sqlite:///{config.DB_PATH}")
    },
    job_defaults={"misfire_grace_time": 60},  # allow 60s late fires on restart
)
scheduler.start()
```

**Job types used in this project:**
- `date` trigger — one-time delayed broadcast at scheduled datetime
- `interval` trigger — pending-application reminder every N minutes
- `date` trigger — payment reminder at T-3 days and T-1 day

---

### DB Migration: Extend Existing Pattern (No New Library)

**Recommendation:** No new library. Extend the existing `_ensure_column()` + `CREATE TABLE IF NOT EXISTS`
pattern in `database/db.py`. Add `PRAGMA user_version` tracking to make migration state auditable.

**Why no Alembic:**
Alembic is tightly coupled to SQLAlchemy ORM. This project uses raw aiosqlite. Adding Alembic would
require adding SQLAlchemy ORM models for all tables, a large refactor with no benefit at this scale.

**Why no yoyo-migrations:**
yoyo-migrations is synchronous (uses DBAPI2 directly, no async support). Would require
`asyncio.get_event_loop().run_in_executor()` wrappers. Adds a dependency for what is 30 lines of
plain Python. At this project's schema complexity and migration cadence (a handful of changes per
milestone), the existing inline pattern is the correct choice.

**The existing pattern already works — extend it:**

`db.py` already contains `_ensure_column(db, table, column, definition)` which wraps
`PRAGMA table_info + ALTER TABLE ADD COLUMN`. This is exactly correct for adding nullable columns
to existing tables without touching existing rows. The pattern to add:

```python
# In init_db(), add new tables using CREATE TABLE IF NOT EXISTS:
await db.execute("""
    CREATE TABLE IF NOT EXISTS coins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT,
        changed_by INTEGER,
        created_at TEXT NOT NULL
    )
""")

# Add new columns to existing tables using _ensure_column():
await _ensure_column(db, "users", "status", "TEXT DEFAULT 'pending'")
await _ensure_column(db, "users", "resume_file_id", "TEXT")
await _ensure_column(db, "users", "payment_status", "TEXT DEFAULT 'not_paid'")
await _ensure_column(db, "users", "receipt_file_id", "TEXT")
```

**Add PRAGMA user_version for migration state tracking (optional but recommended):**

```python
async def _get_schema_version(db) -> int:
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
        return row[0]

async def _set_schema_version(db, version: int):
    await db.execute(f"PRAGMA user_version = {version}")
```

This lets `init_db()` conditionally apply migrations (e.g., run a one-time data backfill only if
`user_version < 2`). No migration table needed — SQLite stores this natively.

---

### Telegram Bot API: getChatMember (No New Library)

**Recommendation:** No new library. `getChatMember` is a native Telegram Bot API method, fully
exposed by aiogram 3 as `await bot.get_chat_member(chat_id, user_id)`.

**Confirmed in aiogram 3.27.0+ docs:**
Returns a `ChatMember` union type. Check subscription status with:

```python
from aiogram.utils.chat_member import MEMBERS
from aiogram.exceptions import TelegramBadRequest

async def is_subscribed(bot: Bot, channel_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return isinstance(member, MEMBERS)
    except TelegramBadRequest:
        # User never interacted with the channel bot or channel is private
        return False
```

`MEMBERS` covers `ChatMemberOwner`, `ChatMemberAdministrator`, `ChatMemberMember`,
`ChatMemberRestricted` (but NOT `ChatMemberLeft` or `ChatMemberBanned`).

**Bot must be administrator in the channel** — this is a Telegram API requirement. The bot cannot
check membership in a channel where it is not an admin. Failing silently (return `False`) is the
correct behavior when the check errors.

**Channel ID format:** pass as `@channel_username` (string) or as negative integer chat_id.
Store the channel username/id in `bot_settings` table using the existing key-value store.

---

### File/Receipt Storage: file_id Pattern (No New Library)

**Recommendation:** No new library. Store Telegram `file_id` as TEXT in the database.
Do NOT download files to disk — file_id is permanent and lets the bot re-send the file at any time.

**Confirmed in aiogram 3 docs:**
`message.document.file_id` (str) — permanent identifier for a file on Telegram's servers.
`message.document.file_name` (str, optional) — original filename as set by sender.
`message.document.mime_type` (str, optional) — for distinguishing PDF vs DOCX.

```python
# In registration handler, when user sends resume:
@router.message(Registration.waiting_for_resume, F.document)
async def handle_resume(message: types.Message, state: FSMContext):
    await state.update_data(resume_file_id=message.document.file_id)
    await state.update_data(resume_file_name=message.document.file_name)
```

**Columns to add to `users` table:**
- `resume_file_id TEXT` — PDF/DOCX resume
- `receipt_file_id TEXT` — payment receipt

**For payment receipts (new `payments` table):**
Store `receipt_file_id` in a dedicated `payments` table alongside `payment_status`, `amount`,
`deadline`, `receipt_sent_at`. This avoids polluting the `users` table with event-specific payment data.

**Admin review:** to show a receipt to admin, `await bot.send_document(admin_id, document=receipt_file_id)`.
No download, no disk I/O, no OCR.

---

## Complete Addition to requirements.txt

```
apscheduler==3.11.2
sqlalchemy>=2.0,<3.0
```

`sqlalchemy` is required by `SQLAlchemyJobStore`. No other new production dependencies.
The existing stack (aiogram, aiosqlite, pydantic-settings, gspread, google-auth, aiohttp-socks)
is unchanged.

---

## Supporting Libraries (Supporting Tables)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| apscheduler | 3.11.2 | Persistent scheduled jobs (broadcasts, reminders) | Required for delayed broadcasts + payment reminders |
| sqlalchemy | >=2.0,<3.0 | APScheduler's SQLAlchemyJobStore backend | Required alongside apscheduler |

Everything else (subscription check, file storage, migrations) is covered by aiogram 3 builtins +
extending existing db.py patterns.

---

## Alternatives Considered

| Recommended | Alternative | Why Not |
|-------------|-------------|---------|
| APScheduler 3.11.2 | APScheduler 4.0 | Still alpha (4.0.0a6). Breaking API changes vs 3.x. Not production-ready as of June 2026. |
| APScheduler 3.x `AsyncIOScheduler` | `BackgroundScheduler` + thread | Background thread cannot safely call `await bot.send_message()` from thread context in asyncio. Requires `run_coroutine_threadsafe` — fragile and error-prone. |
| SQLAlchemyJobStore (SQLite) | Custom job table in aiosqlite | Custom approach requires building serialization, recovery, and idempotency from scratch. SQLAlchemyJobStore is battle-tested. |
| SQLAlchemyJobStore (SQLite) | Redis / PostgreSQL | Requires additional infrastructure (Redis/PG container). Single-instance bot on 1000-user scale does not need distributed coordination. |
| Extend `_ensure_column` pattern | Alembic | Alembic requires SQLAlchemy ORM models for all tables. Massive refactor. Wrong abstraction level for this project. |
| Extend `_ensure_column` pattern | yoyo-migrations | yoyo is synchronous. No async support. Adds a dependency for trivial functionality already present in the codebase. |
| PRAGMA user_version | Custom migrations table | SQLite has a built-in user_version pragma. No extra table needed. |
| aiogram 3 getChatMember built-in | python-telegram-bot | Different framework. Cannot mix with aiogram. |
| file_id in DB column | Download files to disk | Disk storage requires Docker volume management, file naming, cleanup. file_id is permanent, requires no disk I/O, lets bot forward files instantly. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| APScheduler 4.0 | Pre-release alpha; API changed completely from 3.x; no stable release path | APScheduler 3.11.2 |
| `MemoryJobStore` for scheduled broadcasts | Jobs lost on restart/crash; violates the persistence requirement | `SQLAlchemyJobStore` with SQLite |
| `BackgroundScheduler` | Runs in a thread; async job functions (coroutines) cannot be scheduled; bot.send_message must cross thread boundary | `AsyncIOScheduler` |
| Alembic | Requires SQLAlchemy ORM; introduces ORM models for all existing tables; overkill for raw-SQL project | Inline `_ensure_column` + `CREATE TABLE IF NOT EXISTS` pattern |
| yoyo-migrations | Synchronous only; no aiosqlite compatibility; adds dependency for 30-line solution | PRAGMA user_version + inline ALTER TABLE |
| aiojobs | Manages concurrent coroutines, not scheduled delayed jobs; no persistence | APScheduler 3.x |
| Storing files on disk | Requires volume management, naming, cleanup; files deleted if volume unmounted | Store `file_id` TEXT in DB, use Telegram servers as storage |

---

## Version Compatibility

| Package | Compatible With | Notes |
|---------|----------------|-------|
| apscheduler==3.11.2 | sqlalchemy>=1.4,<3.0 | Use sqlalchemy 2.x (current stable 2.0.51). APScheduler 3.x works with both SQLAlchemy 1.4 and 2.x. |
| apscheduler==3.11.2 | Python >=3.8 | Bot requires Python 3.10+ (aiogram constraint); no conflict. |
| sqlalchemy>=2.0 | aiosqlite | aiosqlite is NOT used by APScheduler's job store — APScheduler uses sync SQLAlchemy for job store I/O. aiosqlite remains the async driver for all application DB queries. No conflict. |
| aiogram 3.29.0 | Python >=3.10, <3.15 | No change — already in use. |

---

## Sources

- Context7 `/agronholm/apscheduler` — APScheduler 3.x SQLAlchemyDataStore docs, async scheduler patterns (HIGH confidence)
- Context7 `/websites/aiogram_dev_en_v3_27_0` — getChatMember, ChatMemberStatus, MEMBERS group, document file_id (HIGH confidence)
- [APScheduler PyPI page](https://pypi.org/project/APScheduler/) — confirmed 3.11.2 stable, 4.0.0a6 pre-release (HIGH confidence)
- [APScheduler 3.x user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html) — AsyncIOScheduler + SQLAlchemyJobStore patterns (HIGH confidence)
- [SQLAlchemy PyPI / changelog](https://www.sqlalchemy.org/changelog/CHANGES_2_0_49) — confirmed 2.0.51 stable as of June 2026 (HIGH confidence)
- [aiogram PyPI page](https://pypi.org/project/aiogram/) — confirmed 3.29.0 latest stable June 2026 (HIGH confidence)
- Existing `database/db.py` — existing `_ensure_column` migration pattern already in production (source of truth)

---
*Stack research for: AIESEC Event Bot brownfield extension*
*Researched: 2026-06-25*
