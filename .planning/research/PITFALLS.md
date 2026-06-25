# Pitfalls Research

**Domain:** Telegram event-registration bot — aiogram 3 + aiosqlite + SQLite, adding coins economy, scheduler, approval/payment queue, mass broadcasts at 1000–1500 users/season
**Researched:** 2026-06-25
**Confidence:** HIGH — derived directly from the existing codebase (`db.py`, `admin.py`, `registration.py`, `main.py`), the documented prior-bot failure ("слетали баллы"), and the architectural decisions already recorded in `PLAN_YOULEAD_TZ.md` and `PROJECT.md`.

---

## Critical Pitfalls

### Pitfall 1: Non-Atomic Coins Balance — The Exact Bug That Killed the Prior Bot

**What goes wrong:**
A read-modify-write sequence for coins balance: (1) `SELECT amount FROM coins WHERE user_id = ?`, (2) compute new amount in Python, (3) `UPDATE coins SET amount = ? WHERE user_id = ?`. Two concurrent admin commands — or an admin command racing with a scheduled reward job — can read the same starting balance, both compute deltas independently, and the last writer wins. One delta is silently lost. This is the "слетали баллы" failure mode.

**Why it happens:**
aiosqlite opens a new connection per function call (the current `async with aiosqlite.connect(...)` pattern). Each call is isolated. There is no single shared connection holding a transaction across steps 1-2-3. Aiogram dispatches handlers concurrently in asyncio; two handlers touching the same user's coins can interleave freely.

**How to avoid:**
Never read balance, modify in Python, then write back. Use a single atomic SQL statement:
```sql
UPDATE users_coins SET balance = balance + ? WHERE user_id = ?
```
Or, for the append-only ledger design (which PROJECT.md already mandates):
```sql
INSERT INTO coins (user_id, delta, reason, changed_by, ts) VALUES (?, ?, ?, ?, ?)
```
and compute current balance as `SELECT SUM(delta) FROM coins WHERE user_id = ?`. The `INSERT` itself is atomic; there is no read-modify-write. For "spend" operations (check sufficient balance then deduct), wrap in `BEGIN IMMEDIATE ... COMMIT` within a single aiosqlite connection to serialize the check and the insert.

**Warning signs:**
- Admin reports that a user's balance "jumped back" after receiving coins from two commands in quick succession.
- Unit-testing two concurrent `/coins` commands for the same user produces inconsistent totals.
- Balance computed from `SUM(delta)` in the ledger does not match a cached `balance` column.

**Phase to address:** Phase 1 (coins module) — the table schema and every write path must be correct from day one. Never add a `balance` cache column alongside the ledger; keep balance as a computed `SUM(delta)`.

---

### Pitfall 2: INSERT OR REPLACE Destroys Status and Coins on Re-Registration

**What goes wrong:**
`add_user()` in `db.py` uses `INSERT OR REPLACE INTO users (...)`. SQLite's `REPLACE` is syntactic sugar for `DELETE + INSERT`. When a user re-registers (or when an admin uses the test re-reg button), the entire row is deleted and re-inserted. Any new columns added for this milestone (`status`, `payment_status`, etc.) that are not in the `INSERT` column list will be reset to `NULL` or their default. A user who was `status=approved` becomes `NULL` (effectively unapproved) after a re-registration attempt.

**Why it happens:**
`INSERT OR REPLACE` was fine when the `users` table had no critical state beyond registration data. Once `status`, `payment_status`, and any future FK-linked tables depend on the users row surviving, `REPLACE` becomes destructive.

**How to avoid:**
Replace the `INSERT OR REPLACE` with an explicit upsert: `INSERT INTO users (...) VALUES (...) ON CONFLICT(telegram_id) DO UPDATE SET full_name = excluded.full_name, ...` — listing only the fields that should be overwritten on re-registration, explicitly excluding `status`, `payment_status`, `coins_balance`, etc. Alternatively, split registration into `create_application()` (INSERT only) and `update_application()` (UPDATE only), never using REPLACE.

**Warning signs:**
- An approved user re-sends `/start`, presses "пройти регистрацию заново", and loses their `status=approved`.
- After a test re-registration in staging, the user no longer sees the main menu (because `ensure_registered()` checks `status=approved` and finds `NULL`).

**Phase to address:** Core milestone (approval flow, status field) — the moment `status` is added to the `users` table, `add_user()` must be audited and `INSERT OR REPLACE` removed.

---

### Pitfall 3: Scheduler Jobs Lost on Restart Because MemoryStorage Is the Only Storage

**What goes wrong:**
APScheduler's default `MemoryJobStore` loses all scheduled jobs when the process restarts. A delayed broadcast scheduled for 14:00 that was created at 10:00 disappears if the bot is restarted at 13:50 for a deployment. Payment reminders scheduled "3 days before deadline" are lost after any restart. This is the same failure class as the MemoryStorage FSM problem, applied to jobs instead of state.

**Why it happens:**
`main.py` uses `MemoryStorage()` for FSM. APScheduler's default configuration also keeps jobs in memory. Developers frequently add APScheduler without configuring a persistent job store because the in-memory default "works" in development.

**How to avoid:**
Configure APScheduler with `SQLAlchemyJobStore` pointing at the same SQLite file (or a separate `jobs.db`):
```python
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url="sqlite:///data/jobs.db")},
    timezone="Europe/Moscow"
)
```
Set `misfire_grace_time` appropriately (e.g., 3600 seconds) so jobs that were scheduled during downtime fire immediately on restart rather than being silently skipped. Also persist job metadata (broadcast message content, target filter) in a `scheduled_broadcasts` table in the application DB, not just inside the APScheduler job payload — APScheduler job payloads are opaque pickles, hard to inspect or repair.

**Warning signs:**
- Scheduled broadcast does not fire after a deployment restart.
- APScheduler job list is empty after `scheduler.get_jobs()` call following a restart.
- Payment reminders stop working after the first bot update in production.

**Phase to address:** Phase 2 (scheduled broadcasts + payment reminders) — configure persistence before writing the first scheduled job.

---

### Pitfall 4: Scheduler Timezone Bug in DD.MM.YYYY Date Parsing

**What goes wrong:**
The payment module asks users "when do you plan to pay?" in DD.MM.YYYY format. The scheduler then creates a job to fire a reminder "3 days before that date". If `datetime.strptime("25.06.2026", "%d.%m.%Y")` produces a naive datetime and APScheduler is configured with `timezone="UTC"`, the job fires 3 hours early for Moscow time (UTC+3). For events, this means reminders land in the middle of the night instead of morning.

**Why it happens:**
`datetime.strptime` returns a naive datetime (no timezone info). APScheduler treats naive datetimes as UTC by default when a timezone is configured. The bot's users and admins are in Moscow time. The mismatch is invisible during development in the same timezone.

**How to avoid:**
Parse the date and immediately attach timezone:
```python
import pytz
from datetime import datetime
tz = pytz.timezone("Europe/Moscow")
dt_naive = datetime.strptime(user_input, "%d.%m.%Y")
dt_aware = tz.localize(dt_naive.replace(hour=10, minute=0))  # fire at 10:00 MSK
```
Validate all date inputs in the REG_FLOW `date` type handler: check that the date is in the future, that it is a valid calendar date (catches 31.04, 29.02 in non-leap years), and reject gracefully with a user-facing error.

**Warning signs:**
- Reminders fire 3 hours before or after expected time in production.
- `datetime(2026, 4, 31)` raises `ValueError` at runtime from user input, crashing the handler.
- APScheduler logs show jobs with UTC timestamps when local time was intended.

**Phase to address:** Phase 2 (payment module + scheduled reminders) — enforce at the `date` question-type handler level, not at the scheduler level.

---

### Pitfall 5: Broadcast Rate Limiting — Current sleep(0.05) Hits Telegram's Ceiling and Drops Users

**What goes wrong:**
`admin.py`'s `process_broadcast` loop calls `await message.send_copy(chat_id)` then `await asyncio.sleep(0.05)` — 20 messages/second. Telegram's documented limit is 30 messages/second globally. At 1000 users, the bot is running at 67% of its global budget. Add concurrent admin notifications for new registrations (each registration sends a message to all ADMIN_IDS), a payment reminder job firing simultaneously, and the actual rate exceeds 30/s. Telegram returns HTTP 429 with a `retry_after` value. The current code catches all exceptions identically: the blocked counter increments, the user never gets the broadcast, and there is no retry.

**Why it happens:**
`asyncio.sleep(0.05)` was tuned empirically for small user counts. The code has no `RetryAfter` / `TelegramRetryAfter` exception branch. aiogram's `TelegramRetryAfter` carries the required wait time as `.retry_after`, but the bare `except Exception: blocked += 1` discards it.

**How to avoid:**
```python
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

for chat_id in users_ids:
    while True:
        try:
            await message.send_copy(chat_id)
            count += 1
            await asyncio.sleep(0.035)  # ~28 msg/s, leaves headroom
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            blocked += 1  # user blocked the bot — genuine skip
            break
        except Exception as e:
            logger.error(f"Broadcast failed for {chat_id}: {e}")
            blocked += 1
            break
```
Separate "blocked bot" (permanent, don't retry) from "rate limited" (transient, wait and retry) from "other error" (log and skip).

**Warning signs:**
- Broadcast summary shows more "недоступно" than actual blocked users (when comparing against prior broadcasts).
- Bot.log shows flood of `TelegramRetryAfter` exceptions during broadcast.
- broadcast completes suspiciously fast (all rate-limited users counted as blocked).

**Phase to address:** Core milestone (broadcast infrastructure) — fix before any scheduled or filtered broadcasts are built on top of this foundation.

---

### Pitfall 6: Concurrent Managers Double-Approving the Same Application

**What goes wrong:**
Two managers open the approval queue simultaneously. Both see "Заявка 3/47" (same application). Manager A clicks "Одобрить". The handler reads `status=pending`, sets it to `approved`, sends the approval message to the user. 200ms later, Manager B clicks "Одобрить" on the same message. The handler reads `status=approved` (already set), but if the guard is a Python-level check (`if user['status'] == 'pending'`) done in separate DB calls, a race is possible. More critically, even with a proper DB check, the user receives two "вы одобрены!" messages.

**Why it happens:**
The approval queue UI shows the same application to all managers simultaneously. Without a "claim" mechanism or atomic compare-and-swap at the DB level, two concurrent approvals are possible.

**How to avoid:**
Use a single atomic SQL UPDATE as the gate:
```sql
UPDATE users SET status = 'approved' WHERE telegram_id = ? AND status = 'pending'
```
Check `cursor.rowcount`: if 0, someone else already approved it — silently skip the notification. This is the only safe pattern; a Python-level `if status == 'pending'` check followed by a separate UPDATE has a TOCTOU window.

For "Одобрить все": show a confirmation dialog ("Одобрить 47 заявок?"), then execute in a loop with the same atomic UPDATE guard. Store the count of actually-changed rows and report to the manager.

**Warning signs:**
- User reports receiving two approval messages.
- Admin sees a user marked approved twice in the audit log.
- `status` transitions to `approved` but `rowcount == 0` (guard working correctly).

**Phase to address:** Core milestone (approval flow) — build the atomic UPDATE guard into `approve_user()` from the first implementation.

---

### Pitfall 7: Approval Queue Pagination State Stored in MemoryStorage Is Lost on Restart

**What goes wrong:**
If the current-page index for the "тиндер" approval queue is stored in FSM state (MemoryStorage), a bot restart resets the manager's position to page 1. More subtly, if the manager has processed applications 1-20 and the bot restarts, they see application 1 again — if they click "Одобрить" thinking it is new, they double-approve already-processed items.

**Why it happens:**
`MemoryStorage` is the configured FSM backend. It is fast and simple but non-persistent. The README explicitly notes: "При перезапуске бота все незавершённые состояния сбрасываются."

**How to avoid:**
Do not store pagination state in FSM. Instead, paginate by DB query: always query the next `status=pending` application ordered by `registration_date` (or a stable ID). The "current card" is always the oldest unprocessed application for that manager. No page number to lose. If two managers are active, they will naturally see different applications after each act on one (the first to act removes it from the `status=pending` pool).

Alternative: store a `reviewed_at` nullable timestamp on applications; each manager's "skip" stores their skip in a `manager_skips` junction table so the same manager does not see the same skipped application again.

**Warning signs:**
- After deploying a bot update, managers start seeing applications they already processed.
- Admin reports "I had to re-review everything after the bot restarted."
- The approval queue shows total count > actual pending count.

**Phase to address:** Core milestone (approval flow) — design the pagination query first, before building the inline keyboard handlers.

---

### Pitfall 8: SQLite Migration Without Versioning Accumulates Drift

**What goes wrong:**
The current `init_db()` uses `_ensure_column()` to add columns if missing. This works for additive column additions but has no version tracking. As the codebase grows, `init_db()` accumulates a long list of `_ensure_column` calls. If a column is renamed or a constraint is added, there is no way to detect that an existing DB already has the old column with different semantics. The `coins` table, `status` field, `payment_status`, and `scheduled_broadcasts` table are all being added in this milestone — without versioning, a partially-applied migration on a production DB is undetectable.

**Why it happens:**
The `_ensure_column` pattern is pragmatic and works for the current scale. It becomes a liability when multiple concurrent developers add migrations, or when a production DB is partially updated and the bot is rolled back.

**How to avoid:**
Add a `schema_version` row to `bot_settings` (or a dedicated `schema_migrations` table with applied migration IDs). At startup, run only migrations with version > current. Each migration is a named function: `migrate_v2_add_coins_table()`, `migrate_v3_add_status_field()`. This does not require Alembic — a simple integer version counter with a list of migration functions is sufficient for this scale.

For this milestone specifically: test every migration against a copy of the production DB (`data/forum.db`) before deploying. The `INSERT OR REPLACE` issue (Pitfall 2) and a status field addition interact — validate that existing approved users survive migration.

**Warning signs:**
- `init_db()` file has >10 `_ensure_column` calls with no ordering guarantee.
- A production deployment fails with "table already exists" or "column already exists" because a previous partial deploy left the DB in an unknown state.
- Rollback of the bot binary leaves the DB schema ahead of the code.

**Phase to address:** Core milestone (DB schema additions for status + coins) — introduce schema versioning at the start of the first migration, not after.

---

### Pitfall 9: getChatMember Called Per-Message Without Caching Causes Rate Limits and Latency

**What goes wrong:**
If the channel subscription gate calls `bot.get_chat_member(channel_id, user_id)` on every `/start` or every button press, at 50 concurrent users pressing buttons, that is 50 API calls per second to Telegram. Telegram's Bot API has per-method rate limits (getChatMember is roughly 20-30 calls/second before hitting limits). At 1000 users onboarding in a short window (opening registration), this creates a bottleneck. Additionally, if the bot is not an admin in the channel, `getChatMember` returns `TelegramBadRequest` silently for private channels — the code must handle this as "cannot verify, allow through" rather than blocking all users.

**Why it happens:**
`getChatMember` is simple to call and appears cheap. The rate limit only manifests under load.

**How to avoid:**
Cache the result per user with a TTL (60–300 seconds is standard):
```python
_subscription_cache: dict[int, tuple[bool, float]] = {}

async def is_subscribed(bot: Bot, channel_id: int, user_id: int) -> bool:
    if user_id in _subscription_cache:
        result, ts = _subscription_cache[user_id]
        if time.time() - ts < 300:  # 5-minute TTL
            return result
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        result = member.status not in ("left", "kicked", "banned")
    except Exception:
        result = True  # fail-open: don't block users when check fails
    _subscription_cache[user_id] = (result, time.time())
    return result
```
For this scale, an in-process dict cache is sufficient (the bot runs as a single process). The cache resets on restart, which is acceptable — users re-check on next interaction.

**Warning signs:**
- Bot logs show repeated `getChatMember` calls for the same user within seconds.
- Users report slowness at `/start` during registration peaks.
- `TelegramBadRequest: member list is inaccessible` errors when the channel is private and the bot is not admin.

**Phase to address:** Phase 1 (channel subscription gate) — build the cache in from the start.

---

### Pitfall 10: FSM Dropout Tracking Requires DB Persistence, Not MemoryStorage

**What goes wrong:**
The "напоминание о дорегистрации" feature requires tracking users who started the registration flow but did not finish. The natural implementation is to check FSM state: "who is currently in `Registration.*` state?" But `MemoryStorage` does not support querying all active states by state type — it is a key-value store keyed by (chat_id, user_id). Even if it did, the states vanish on restart, so any user who dropped out during a downtime window is invisible. A scheduler job that runs every N hours needs a persistent list of "started but not finished" users.

**Why it happens:**
MemoryStorage is the configured backend. The FSM API does not expose "list all users in state X" because MemoryStorage does not index by state. This seems like it should work until someone tries to implement it.

**How to avoid:**
Track registration start in the DB, not in FSM. When a user passes the full_name step (or any step past `/start`), write `INSERT OR IGNORE INTO reg_started (telegram_id, started_at) VALUES (?, ?)`. When registration completes (`finalize_registration`), `DELETE FROM reg_started WHERE telegram_id = ?`. The scheduled reminder job queries `SELECT telegram_id FROM reg_started WHERE started_at < datetime('now', '-1 hour')` and sends the push. This is restart-safe and queryable.

Do not use `RedisStorage` or `MongoStorage` just to make FSM queryable — for this project's scale, the DB-tracking pattern above is simpler and has no new dependencies.

**Warning signs:**
- Attempts to list users in `Registration` FSM state return empty or raise `NotImplementedError`.
- After a bot restart, the dropout reminder job sends zero reminders despite known dropouts.
- The feature "works" in local testing (no restarts) but fails silently in production.

**Phase to address:** Phase 2 (FSM dropout reminders) — create the `reg_started` table as part of the same migration that adds scheduled jobs.

---

## Moderate Pitfalls

### Pitfall 11: file_id for Receipts Becomes Invalid Across Bot Token Changes

**What goes wrong:**
`file_id` values in Telegram are stable for the lifetime of a bot token. If the bot token is rotated (compromised token, new bot for a new season), all stored `file_id` values become invalid. Calling `bot.send_document(chat_id, file_id)` with an old token's file_id raises `TelegramBadRequest: wrong file identifier`.

**How to avoid:**
For receipts and resumes that must survive token rotation, download and store the file locally or in object storage during intake. For this project's scale (local SQLite, single server), storing in `data/files/<telegram_id>_receipt.pdf` is sufficient. Always store both `file_id` (for fast resending within the same token lifetime) and `local_path` (for recovery). Validate file type at intake: check `message.document.mime_type` against `["application/pdf"]` for receipts; reject `image/*` disguised as `.pdf` by filename.

**Warning signs:**
- Manager trying to review a receipt sees "wrong file identifier" error.
- After a season reset (new bot token for next event), all previously uploaded receipts are inaccessible.

**Phase to address:** Phase 2 (payment module + receipt intake).

---

### Pitfall 12: Approval "Notify All Admins" Creates Notification Storms at 1000+ Applications

**What goes wrong:**
`finalize_registration()` currently loops over all `ADMIN_IDS` and sends each admin a notification for every new registration. With `approval_mode=manual`, this loop fires for every application submission. At peak registration (50 submissions in an hour), each of 3-5 admins receives 50 messages. This is the notification-per-application pattern that PLAN_YOULEAD_TZ.md explicitly rejected for the queue UI — but it still exists in the notification path.

**How to avoid:**
Replace per-application notifications with the periodic scheduled "⏳ 47 заявок ждут обработки" reminder that PROJECT.md already specifies. Send this reminder once every N hours (configurable). Suppress the immediate per-application push, or make it a configurable toggle (default off for `approval_mode=manual`, default on for `approval_mode=auto` where the admin notification is the primary record).

**Warning signs:**
- Admins receive dozens of notifications in a short window and mute the bot.
- Admin notification messages hit the 30 msg/sec global rate limit during a registration spike.

**Phase to address:** Core milestone (approval flow) — when splitting `finalize_registration()` into `submit_application()` + `approve_user()`, remove or gate the per-submission admin notification.

---

### Pitfall 13: APScheduler Duplicate Job Registration on Every Bot Restart

**What goes wrong:**
A common implementation mistake: the bot registers scheduled jobs in `main()` unconditionally. With a persistent job store (SQLiteJobStore), jobs survive restarts. Running `scheduler.add_job(...)` again on the next restart creates a duplicate job with a new ID, so the payment reminder fires twice. After 10 restarts, a user gets 10 reminder messages.

**Why it happens:**
`add_job` does not check for existing jobs with the same logical ID unless `id=` is specified and `replace_existing=True` is used.

**How to avoid:**
Always specify `id=` for recurring/system jobs and use `replace_existing=True`:
```python
scheduler.add_job(
    send_pending_queue_reminder,
    "interval", hours=4,
    id="pending_queue_reminder",
    replace_existing=True
)
```
For user-specific jobs (payment reminder for user 12345 with deadline 2026-07-01), use a deterministic ID: `id=f"payment_reminder_{user_id}_{deadline_date}"`. Check `scheduler.get_job(job_id)` before adding to avoid unnecessary `replace_existing` churn.

**Warning signs:**
- User reports receiving duplicate reminders.
- `scheduler.get_jobs()` returns multiple jobs with identical names but different IDs.
- Reminders multiply after each deployment.

**Phase to address:** Phase 2 (scheduler setup) — establish the `id=` + `replace_existing=True` convention before any user-facing jobs are created.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| `INSERT OR REPLACE` for all user writes | Simpler upsert logic | Destroys status/coins fields on any re-registration | Never acceptable once status fields exist |
| `MemoryStorage` for FSM | Zero setup | All mid-flow users lose state on restart; dropout tracking impossible | Acceptable for registration flow itself; never for dropout tracking |
| `asyncio.sleep(0.05)` in broadcast | Fast delivery | Hits rate limits at 1000+ users; no retry on 429 | Replace before adding scheduled broadcasts |
| Per-application admin notifications | Manager sees real-time updates | Notification storm at scale; admins mute the bot | Acceptable for auto-approval mode only |
| `_ensure_column` without versioning | Simple additive migrations | No way to detect partial migrations or non-additive changes | Acceptable up to ~5 migrations; add versioning at migration 6+ |
| In-process dict cache for getChatMember | No new dependencies | Cache lost on restart; no invalidation for kicked users | Acceptable for this scale and usage pattern |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Telegram getChatMember | Assuming it works for all channel types without bot being admin | Check for `TelegramBadRequest` on private channels; fail-open (allow user through) rather than fail-closed |
| APScheduler + aiosqlite | Running `scheduler.start()` before `await init_db()` | `init_db()` must complete before scheduler starts; jobs may reference DB functions that require initialized tables |
| APScheduler + asyncio | Using `BlockingScheduler` instead of `AsyncIOScheduler` | `AsyncIOScheduler` is required in an aiogram 3 async process; `BlockingScheduler` blocks the event loop |
| Google Sheets in broadcast/approval flow | Calling `append_to_sheet()` from within a broadcast loop or approval loop | Sheets calls are slow (1-2s); never call from within a tight loop; use a background task or queue |
| aiogram `send_copy` vs `forward_message` | Using `forward_message` for broadcasts | `forward_message` adds "Forwarded from:" header and looks unprofessional; `send_copy` (or `copy_message`) is the correct method for broadcast |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| `get_all_users_ids()` loads full ID list into memory before broadcast | Memory spike at broadcast start | Acceptable for 1500 users (~12KB); use cursor-based iteration for >50K | Never an issue at current scale |
| 19 `get_setting()` calls per registration step (one per enabled question check) | Slow step transitions during peak registration | Cache settings in-process with a short TTL (30s); invalidate on `set_setting()` | Noticeable at 50+ concurrent registrations |
| `getChatMember` per user per `/start` without cache | Slow `/start` response under load | Per-user cache with 5-minute TTL | 20+ concurrent `/start` calls |
| Approval queue renders all pending applications to count them on every page load | Slow admin UI response | Use `SELECT COUNT(*) WHERE status='pending'` rather than fetching all rows | At 500+ pending applications |
| SQLite write contention: all DB writes serialize behind each other | Broadcast loop slows down as DB writes from concurrent registrations queue up | Enable WAL mode: `PRAGMA journal_mode=WAL` in `init_db()` — allows concurrent reads and one writer | At 20+ concurrent users writing simultaneously |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Admin check via `message.from_user.id in config.ADMIN_IDS` can be bypassed if `ADMIN_IDS` is loaded from a mutable source | Admin escalation if a user can manipulate config | `ADMIN_IDS` comes from `.env` (pydantic-settings) — immutable at runtime. No action needed. |
| File receipts accepted without MIME type check | Malicious file disguised as PDF | Check `message.document.mime_type == "application/pdf"` before storing; reject others with clear error |
| `file_id` exposed in admin logs | Tokens for sensitive files (receipts, resumes) logged to `bot.log` | Log file types and counts, not `file_id` values |
| `INSERT OR REPLACE` on `add_user` allows a malicious re-registration to reset another user's `status` | User circumvents approval queue | Fixed by switching to `ON CONFLICT DO UPDATE SET` (Pitfall 2 prevention) |

---

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Approval notification sent only once; user forgets they applied | User re-registers thinking the first attempt failed; creates duplicate applications | Detect re-registration of `status=pending` user; show "заявка на рассмотрении" message instead of restarting flow |
| "Одобрить все" fires with no confirmation | Manager fat-fingers the button, all 47 applications approved including ones they intended to review | Show "Подтвердить: одобрить 47 заявок?" confirmation step with 10-second timeout before executing |
| Rejection notification gives no reason | User is confused, re-submits immediately, increases queue load | Rejection handler prompts manager for a reason (optional text); reason included in rejection message to user |
| Payment reminder sent before user has seen the payment instructions | User gets a reminder for a payment they do not know how to make | Only schedule reminders after payment instructions have been sent (after `receipt_instructions_sent` flag is set) |
| FSM dropout reminder sent to a user who already completed registration on another device | User gets a confusing "finish your registration" push after they are already registered | Check `status != NULL` in `reg_started` query before sending reminder; delete `reg_started` row on any successful completion |

---

## "Looks Done But Isn't" Checklist

- [ ] **Coins module:** Verify that concurrent `/coins @user +5` commands produce correct balance — run two simultaneous commands and check `SUM(delta)` equals expected total. If using `INSERT` into ledger, this is inherently correct; if using `UPDATE balance = balance + ?`, it is not.
- [ ] **Approval flow:** Verify that clicking "Одобрить" twice on the same application (simulating two managers) sends exactly one approval message to the user. Check `rowcount` from the atomic UPDATE.
- [ ] **Broadcast:** After a broadcast to 1000 users, verify the "недоступно" count matches real blocked users (not rate-limited users). Check bot.log for `TelegramRetryAfter` exceptions.
- [ ] **Scheduler persistence:** Schedule a job, restart the bot, verify the job still exists via `scheduler.get_jobs()`. Check it fires at the correct time.
- [ ] **Timezone:** Schedule a test job for 1 minute in the future using a DD.MM.YYYY date parsed as Moscow time. Verify it fires at the correct UTC time.
- [ ] **Migration:** Apply all migrations to a copy of the production DB. Verify that existing `status=approved` users are not reset to `NULL` after `init_db()` runs.
- [ ] **getChatMember:** Temporarily make the bot non-admin in the test channel. Verify that `/start` does not crash and fails-open (user can proceed).
- [ ] **Receipt intake:** Send a `.jpg` renamed to `.pdf`. Verify the bot rejects it based on MIME type, not filename.
- [ ] **Dropout reminders:** Complete a registration in a separate account. Verify that account does NOT appear in the `reg_started` table after completion.

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Points lost (non-atomic update) | HIGH | Reconstruct balance from bot.log or Google Sheets; manually INSERT corrective transactions into the ledger with `reason="manual_correction"` |
| INSERT OR REPLACE wiped status fields | MEDIUM | Restore from the most recent DB backup; re-apply approvals from Google Sheets export |
| Broadcast sent duplicate messages (retry bug) | LOW | No technical recovery; send apology message; fix retry logic |
| Scheduler jobs lost on restart (no persistence) | MEDIUM | Re-create all pending jobs manually via admin command; implement persistence immediately |
| Two managers double-approved same user | LOW | Send clarifying message to user; add `UNIQUE` constraint on (telegram_id, status, approved_by) or rely on rowcount=0 guard |
| Duplicate reminder jobs after restart | LOW | `scheduler.remove_all_jobs()` + restart; add `replace_existing=True` and redeploy |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Non-atomic coins balance | Phase 1 — coins module | Concurrent command test; check ledger SUM |
| INSERT OR REPLACE destroys status | Core milestone — status field + add_user() refactor | Migration test against prod DB copy |
| Scheduler jobs lost on restart | Phase 2 — scheduler setup | Restart test with pending job |
| Timezone bug in date parsing | Phase 2 — payment date question type | Unit test with Moscow date, verify UTC job time |
| Broadcast rate limiting / no retry | Core milestone — broadcast hardening | 1000-user load test; check blocked vs rate-limited counts |
| Concurrent approval double-fire | Core milestone — approve_user() | Two-manager simultaneous approval test; check rowcount |
| Pagination state lost on restart | Core milestone — approval queue | Restart bot mid-review; verify queue starts from oldest pending |
| SQLite migration without versioning | Core milestone — first DB migration | Check schema_version increments correctly |
| getChatMember rate limit + no-admin failure | Phase 1 — subscription gate | Remove bot from channel, verify fail-open behavior |
| FSM dropout tracking requires DB | Phase 2 — dropout reminders | Restart bot mid-registration; verify reg_started row survives |
| file_id invalidation on token rotation | Phase 2 — receipt intake | Document the limitation; add local_path storage alongside file_id |
| Admin notification storm | Core milestone — submit_application() | Register 10 users in quick succession; verify admins receive reminder, not 10 individual pushes |
| Duplicate APScheduler jobs on restart | Phase 2 — scheduler setup | Restart bot 3 times; verify scheduler.get_jobs() returns exactly one job per logical job |

---

## Sources

- Existing codebase: `database/db.py` (INSERT OR REPLACE pattern, aiosqlite connection-per-call), `handlers/admin.py` (broadcast loop with sleep(0.05), bare except), `handlers/registration.py` (MemoryStorage FSM, finalize_registration), `main.py` (MemoryStorage configuration)
- `PLAN_YOULEAD_TZ.md`: documents "слетали баллы" failure, pagination decision rationale, approval queue design
- `PROJECT.md`: scale context (1000–1500 users/season, 590 active from 1072), known constraints (MemoryStorage resets on restart), key decisions
- Telegram Bot API docs: getChatMember rate limits, 30 msg/sec global limit, TelegramRetryAfter behavior, file_id stability per token
- APScheduler docs: SQLAlchemyJobStore, misfire_grace_time, replace_existing, AsyncIOScheduler for asyncio loops
- SQLite docs: `INSERT OR REPLACE` = DELETE + INSERT semantics, WAL mode, `BEGIN IMMEDIATE` for serialized read-modify-write
- aiogram 3 docs: `TelegramRetryAfter` exception with `.retry_after` attribute, `TelegramForbiddenError` for blocked users

---
*Pitfalls research for: aiogram 3 + aiosqlite + SQLite Telegram event-registration bot — coins, approval queue, scheduler, broadcast at 1000+ users*
*Researched: 2026-06-25*
