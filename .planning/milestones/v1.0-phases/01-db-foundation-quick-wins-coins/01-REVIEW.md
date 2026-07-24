---
phase: 01-db-foundation-quick-wins-coins
reviewed: 2026-07-24T00:00:00Z
depth: deep
files_reviewed: 4
files_reviewed_list:
  - database/db.py
  - handlers/registration.py
  - handlers/user_actions.py
  - handlers/admin.py
findings:
  critical: 0
  high: 2
  medium: 3
  low: 5
  total: 10
status: issues_found
---

# Phase 1: Code Review Report — DB Foundation + Quick Wins + Coins

**Reviewed:** 2026-07-24
**Depth:** deep (cross-file: db.py ↔ registration.py ↔ user_actions.py ↔ admin.py)
**Files Reviewed:** 4 source files
**Status:** issues_found

## Overall Verdict

**CONDITIONAL PASS — ship the DB/coins core, fix the subscription feature.**

The DB migration layer, the append-only coins ledger, the `add_user` `ON CONFLICT DO UPDATE`
guards, and the SQL-injection posture (`_assert_identifier` + column whitelist + fully
parameterized values) are genuinely solid and defensively written. No SQL injection, no
secret leakage, no data-loss on re-registration were found — the `status` / `resume_*` /
`payment_*` preservation logic is correct.

However, the **subscription-tracking feature (QW-02) is effectively non-functional in
production** due to two independent defects: (1) the flag is written *before* the user row
exists on first `/start`, so it silently no-ops for every first-touch registrant, and
(2) the check targets `contact_tg` — an admin-entered channel *link/URL*, not the
`@username`/chat-id that `get_chat_member` requires — so the check fails-open for the common
URL form. Together these mean the "🚫 Не подписаны на канал" broadcast segment is
permanently (or near-permanently) empty. Neither crashes the bot (fail-open by design), but
the deliverable does not do what it claims.

Everything else is Medium/Low: a fire-and-forget task that can be GC'd, a plan/impl mismatch
(no `PRAGMA user_version`), and some quality/dead-code items.

---

## High

### HG-01: Subscription flag is lost on first `/start` — `set_user_subscribed` UPDATEs a row that doesn't exist yet

**File:** `handlers/registration.py:1319-1326` (call site) + `database/db.py:637-643` (`set_user_subscribed`)

**Issue:** In `cmd_start`, the subscription check runs at the very top of the handler
(lines 1319-1326), *before* `user = await get_user(user_id)` (line 1357) and long before the
user is ever inserted via `add_user` in `finalize_registration`. `set_user_subscribed` is a
bare `UPDATE users SET subscribed = ? WHERE telegram_id = ?` (db.py:639-642). For a
first-ever `/start` (the exact moment most users register), no `users` row exists yet, so the
UPDATE matches **0 rows and the subscription result is silently discarded**. The flag is only
ever persisted on a *subsequent* `/start` by an already-registered user. Since the majority of
users `/start` once (to register) and then navigate via menu buttons, `subscribed` stays
`NULL` for them forever. `get_non_subscriber_ids` (db.py:646-651) filters `WHERE subscribed = 0`,
so these users are invisible to the "не подписаны на канал" segment. The feature systematically
under-reports non-subscribers.

**Fix:** Persist the subscription result *after* the user row is guaranteed to exist, or make
the write an upsert. Simplest correct fix — move the flag write to the end of
`finalize_registration` (after `add_user`), and/or re-check on menu entry. Alternatively make
`set_user_subscribed` idempotent so an early call is not lost:

```python
# db.py — upsert so an early write survives the later add_user
async def set_user_subscribed(telegram_id: int, subscribed: bool):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (telegram_id, subscribed) VALUES (?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET subscribed = excluded.subscribed",
            (telegram_id, 1 if subscribed else 0),
        )
        await db.commit()
```
Note: an upsert here creates a bare `users` row for a not-yet-registered user — verify that
does not pollute `get_all_users_dicts`/stats. The cleaner fix is to defer the write to after
`add_user` in `finalize_registration` and re-run the check on an authenticated menu action.

### HG-02: Subscription check points at `contact_tg` (a channel link/URL), which `get_chat_member` cannot resolve → permanent fail-open

**File:** `handlers/registration.py:1320-1324` + `is_subscribed` `registration.py:1301-1308`

**Issue:** `channel = await get_setting("contact_tg")` and then
`await bot.get_chat_member(channel, user_id)`. `contact_tg` is defined in the admin settings
UI as a free-text **link**: `admin.py:347` prompts *"Введите ссылку на Telegram-канал"* and it
is rendered as `TG: {contact_tg}` in the contacts screen — i.e. admins paste
`https://t.me/aiesecforum`. Telegram's `getChatMember` accepts a numeric chat id or a
`@username`, **not** a `t.me/...` URL. Passing the URL raises, `is_subscribed` catches it and
returns `None` (fail-open), so `set_user_subscribed` is never called. The bot's own tests use
`@somechannel` / `@c`, confirming the intended input format the code never normalizes. Result:
for the common URL configuration the entire subscription feature is a silent no-op regardless
of HG-01.

**Fix:** Use a dedicated setting for the check target (not the display link) and/or normalize
the value to a `@username` before calling the API:

```python
def _normalize_channel(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    # accept @name, t.me/name, https://t.me/name, joinchat kept out (private → needs id)
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{5,})$", raw)
    if m:
        return "@" + m.group(1)
    return raw if raw.startswith("@") else None
```
Then read a purpose-built `subscription_channel` setting first, falling back to a normalized
`contact_tg`. Also document that the bot must be an admin of the channel for `getChatMember`
to succeed.

---

## Medium

### MD-01: Fire-and-forget sheet-append tasks are not strongly referenced — may be garbage-collected before running

**File:** `handlers/registration.py:2294, 2297`

**Issue:** `asyncio.create_task(append_to_party_sheet(_party_row))` and
`asyncio.create_task(append_to_sheet(_sheet_row))` are created with no strong reference held.
The event loop keeps only a *weak* reference to a task, so it can be collected mid-run. This is
the exact hazard the codebase documents and mitigates in `main.py:51-61` (`WR-02`, `_spawn`
holds strong refs in `_background_tasks`), but the mitigation is not applied here. Under GC
pressure a registration can complete without the row ever reaching the Google Sheet — a silent
data-sync loss, with no error logged.

**Fix:** Reuse the same strong-ref pattern (expose/import `_spawn`, or keep a module-level
`set`):

```python
_sheet_tasks: set[asyncio.Task] = set()
def _spawn_sheet(coro):
    t = asyncio.create_task(coro)
    _sheet_tasks.add(t)
    t.add_done_callback(_sheet_tasks.discard)
    return t
# ...
_spawn_sheet(append_to_sheet(_sheet_row))
```

### MD-02: `get_incomplete_user_ids` / "не завершили регистрацию" segment can target fully-registered users

**File:** `database/db.py:597-600` + `handlers/admin.py:1411-1421`

**Issue:** The incomplete segment blasts every `telegram_id` in `reg_started`. That row is
deleted on completion via `clear_reg_started` — but that call is deliberately fail-soft
(`registration.py:2261-2264`, logs and swallows errors). Any failed/lost `clear_reg_started`
(DB locked, crash between `add_user` and the clear, etc.) leaves a stale row, so a *completed,
approved* user can receive a "you didn't finish registering" nudge. There is no join against
`users` to exclude finished registrants.

**Fix:** Exclude users who already have a completed `users` row:

```sql
SELECT r.telegram_id FROM reg_started r
LEFT JOIN users u ON u.telegram_id = r.telegram_id
WHERE u.telegram_id IS NULL
```

### MD-03: `mark_reg_started` resets `started_at` on every repeat `/start` → dropout nudge can be deferred indefinitely

**File:** `database/db.py:566-577`

**Issue:** The `ON CONFLICT(telegram_id) DO UPDATE SET ... started_at=excluded.started_at`
rewrites the start timestamp each time an in-progress user re-issues `/start`. Because
`get_nudge_candidates` (db.py:783-791) selects `WHERE started_at < ? AND nudged_at IS NULL`, a
user who repeatedly restarts the flow keeps pushing their `started_at` forward and is never old
enough to be nudged. Dropout analytics (`get_dropout_step_stats`) are also skewed toward the
latest restart.

**Fix:** Only set `started_at` on first insert; preserve it on conflict
(`started_at=reg_started.started_at`) and update only `username`/`participant_type`. If a
"latest activity" timestamp is wanted, add a separate `updated_at` column rather than
overwriting the dropout anchor.

---

## Low

### LW-01: `PRAGMA user_version` is specified by the stack/plan but never implemented

**File:** `database/db.py:37-182` (`init_db`)

**Issue:** CLAUDE.md and the phase stack call for `PRAGMA user_version` versioning of
migrations, but `init_db` relies solely on `CREATE TABLE IF NOT EXISTS` + idempotent
`_ensure_column`. The additive pattern is safe and idempotent, so this is not a runtime bug —
but it is a documented deliverable that is absent, and there is no schema-version marker for
future destructive migrations to key off.

**Fix:** Either implement `PRAGMA user_version` gating (bump after each migration batch) or
update the stack doc to state the project intentionally uses pure-idempotent additive
migrations with no version pragma.

### LW-02: `get_user_rank` contains dead code — the first `None` guard is unreachable

**File:** `database/db.py:539-561`

**Issue:** The balance query is `SELECT COALESCE(SUM(delta), 0) ...`, which always returns a
one-row result with a non-NULL value (0 when the user has no rows). Therefore the guard
`if row is None or row[0] is None: return None` (lines 543-544) can never fire; the real
"no ledger rows" case is handled only by the third query (lines 556-560). Two round-trips do
the work of one and the first guard is dead.

**Fix:** Drop the dead guard and collapse to a single existence-aware query, e.g. compute rank
only after confirming rows exist, or use `SELECT SUM(delta)` (nullable) and treat `NULL` as
"no rows → None" in one pass.

### LW-03: Resume validation is extension-only (no MIME/content check)

**File:** `handlers/registration.py:1186-1191` (`_is_allowed_resume`)

**Issue:** `_is_allowed_resume` accepts any file whose *name* ends in `.pdf`/`.docx`. A user
can rename an arbitrary file (e.g. `virus.exe` → `resume.pdf`) and it passes. Risk is low
because the bot only stores/forwards the `file_id` and uploads to Nextcloud (never executes
it), and Telegram already sanitizes, but organizers downloading the file could be misled by the
extension. `message.document.mime_type` is available and unused.

**Fix:** Additionally check `message.document.mime_type` against
`application/pdf` and the DOCX/OOXML type, and cap `message.document.file_size`.

### LW-04: `set_reg_step` keys on `message.chat.id` while `mark_reg_started` keys on `message.from_user.id`

**File:** `handlers/registration.py:536` vs `registration.py:1205`

**Issue:** Dropout tracking mixes identity sources: the row is created with `from_user.id` but
the step is stamped with `chat.id`. These are equal in private chats (the only place
registration runs today), so it is currently correct — but the inconsistency is a latent bug if
registration is ever driven from a group/thread context, where `chat.id != from_user.id` and
the `UPDATE` would silently miss.

**Fix:** Use one identity consistently — prefer `from_user.id` in both, or document why
`chat.id` is intentional for callback-authored messages.

### LW-05: `contact_tg` doubles as user-facing contact link *and* subscription target — coupled concerns

**File:** `handlers/registration.py:1320` + `handlers/user_actions.py:234-247`

**Issue:** The same setting is rendered verbatim to users as a clickable contact link *and*
fed to `get_chat_member`. These have contradictory format requirements (human URL vs API
`@username`/id), which is the root cause of HG-02. Even once HG-02 is fixed by normalization,
overloading one setting for two purposes is fragile — changing the contact display can silently
break subscription tracking and vice-versa.

**Fix:** Introduce a dedicated `subscription_channel` setting (`@username` or numeric id) used
only by the check, leaving `contact_tg` purely for display.

---

## What was checked and found correct (adversarial pass, no defect)

- **`add_user` `ON CONFLICT DO UPDATE`** (db.py:208-353): correctly omits `status`,
  `payment_status`, `payment_option`, `payment_due`, `paid_at` from the UPDATE set and
  `COALESCE`s `resume_*`/`receipt_file_id`, so re-registration does not clobber approval or
  payment state. `subscribed` is neither inserted nor updated → preserved. Verified.
- **Coins ledger** (db.py:498-561): append-only `INSERT`, balance as `COALESCE(SUM(delta),0)`
  — no read-modify-write race on balance. `_parse_coins_amount` (admin.py:84-93) rejects
  empty/Unicode-digit/garbage tokens without raising. Correct.
- **SQL injection posture**: `_assert_identifier` (db.py:18-21) guards every interpolated
  identifier; `_build_filter_clause` / `get_distinct_filter_values` use a hard column whitelist
  and bind all values as `?`. CSV export neutralizes formula injection (`_csv_safe`). No
  injection path found.
- **`_extract_referrer_id` / `_parse_age`** (registration.py:795-814): ASCII-digit-guarded,
  no `int()` crash on Unicode digits, self-referral rejected. Correct.
- **`is_subscribed` fail-open** (registration.py:1301-1308) and the `cmd_start` try/except
  wrappers: never propagate, never crash `/start`. Correct (the *feature* is broken by
  HG-01/HG-02, but the fail-open contract itself holds).
- **Approval atomicity** (`approve_user_atomic`/`reject_user`, db.py:666-686): guarded
  `UPDATE ... WHERE status='pending'` with `rowcount` double-action detection. Correct.
- **Migration safety**: `status TEXT DEFAULT 'approved'`, `payment_status DEFAULT 'not_paid'`,
  `participant_type DEFAULT 'full'` back-fill existing ~590 rows via ALTER-with-DEFAULT.
  Compatible.

---

_Reviewed: 2026-07-24_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
