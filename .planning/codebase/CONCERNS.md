# Codebase Concerns

**Analysis Date:** 2026-07-24

## Tech Debt

**No dependency pinning / no lockfile:**
- Issue: `requirements.txt` pins only `apscheduler==3.11.2`, `sqlalchemy>=2.0,<3.0`, and `gspread>=6.0,<7.0`. Everything else (`aiogram>=3.0.0`, `aiosqlite`, `pydantic-settings`, `python-dotenv`, `aiohttp-socks`, `google-auth`) has no upper bound or no pin at all, and there is no lockfile (`requirements.lock`, `poetry.lock`, `Pipfile.lock`).
- Files: `requirements.txt`
- Impact: A fresh `pip install -r requirements.txt` months from now (e.g. redeploying to a new server, or after a `Dockerfile` rebuild with no cache) can silently pull a breaking major version — most notably a future `aiogram 4.x`, which the project's own `CLAUDE.md` stack notes treat as a hard incompatibility ("Fixed Core Stack — Do Not Change"). Docker builds are not reproducible across time.
- Fix approach: Pin exact versions for all direct dependencies (`pip freeze > requirements.txt` from the working `.venv`, then hand-trim to direct deps only), or add an upper bound on `aiogram` (`>=3.0.0,<4.0.0`) at minimum.

**God-file handlers (`handlers/admin.py`, `handlers/registration.py`):**
- Issue: `handlers/admin.py` is 3120 lines, `handlers/registration.py` is 2446 lines, each mixing many unrelated concerns (stats, settings CRUD, broadcast wizard, moderation queue, sheet-sync tools in admin; FSM engine, Sheets row/header building, party-track logic, every `process_*` step handler in registration). Documented in detail in `ARCHITECTURE.md`'s Anti-Patterns section — not re-derived here.
- Files: `handlers/admin.py`, `handlers/registration.py`
- Impact: Any settings/flow change touches a multi-thousand-line file; hard to locate logic without full-text search; elevated merge-conflict risk when two features land in the same file concurrently.
- Fix approach: See `ARCHITECTURE.md` — group new code near existing similarly-scoped blocks rather than appending at file end; do not attempt a full module split mid-feature.

**Settings validated only at read time, parsed inconsistently across call sites:**
- Issue: `bot_settings` values are free-form strings; each consumer (`services/reminders.py::_reminder_interval`, `services/scheduler.py::_int_or_default`, various inline `== "on"` checks scattered through `handlers/admin.py`) re-implements its own parse/default logic rather than sharing one schema/validator.
- Files: `services/reminders.py`, `services/scheduler.py`, `handlers/admin.py`, `database/db.py` (`get_setting`/`set_setting`)
- Impact: A newly added call site that copies the wrong parsing idiom (e.g. treats `None` as `"off"` instead of `"on"` by default, or vice-versa) creates a silent behavior mismatch between what the admin UI displays and what the scheduler/handler actually does — already flagged once in this codebase (`_reminder_enabled`'s docstring explicitly calls out "unknown -> True (default on)").
- Fix approach: Before adding a new `get_setting(key)` call for a key that already has a parser helper, grep for the key name and reuse the existing helper. Consider centralizing all default/parse logic per key in one lookup table if the number of settings keeps growing (currently ~70 settings tracked in `admin-config-backlog` memory).

**No shared DB test isolation (`config.DB_PATH` is a mutable module global):**
- Issue: Every test file redefines its own local `_use_tmp_db(tmp_path)` helper that mutates `config.DB_PATH` in place; there is no `conftest.py`-level fixture enforcing isolation across parallel/out-of-order test runs.
- Files: `tests/test_*.py` (all 33 files), `config.py`
- Impact: Running tests in parallel (`pytest -n auto`) or via a test runner that reorders files could cause cross-test DB-path collisions since `config.DB_PATH` is shared global state. Currently safe only because tests run serially (default `pytest` invocation).
- Fix approach: Add a `conftest.py` with an `autouse` fixture that saves/restores `config.DB_PATH` around each test, or migrate to `pytest-xdist`-safe isolation before enabling parallel test execution.

**`docker-compose.yml` has an invalid `version` value:**
- Issue: `version: 'version'` — the literal string `"version"` instead of a compose-file schema version like `"3.8"`.
- Files: `docker-compose.yml:1`
- Impact: Modern `docker compose` (v2 CLI) ignores the deprecated `version` key entirely so this currently causes no functional failure, only a stray/confusing value if anyone inspects the file; older `docker-compose` (v1, if still used anywhere) may warn or error on an unparseable version string.
- Fix approach: Either remove the `version:` key (compose v2 doesn't need it) or set a real value (`"3.8"`).

## Known Bugs

**None identified as currently reproducible/open.** The codebase's inline comments (`# WR-08:`, `# ME-03:`, `# QW-03:`, `# IN-0x:`, etc.) document a long history of *already-fixed* bugs with the fix rationale left in place — this is a deliberate audit-trail convention (see `CONVENTIONS.md`), not a sign of unresolved issues. `.planning/IMPROVE-LOG.md` currently tracks one item as `in_progress` (admin UI for `approve_text__party` + track-switcher; not a bug, a UI-completeness gap) and no items are marked broken.

## Security Considerations

**Nextcloud TLS verification defaults to OFF:**
- Risk: `NEXTCLOUD_VERIFY_TLS: bool = False` is the hardcoded default in `config.py`; `services/nextcloud.py::_put_bytes` passes `ssl=False` to `aiohttp` whenever this flag is unset, disabling certificate validation for the WebDAV upload of resumes (which may include applicant PII — name, resume file contents).
- Files: `config.py:46`, `services/nextcloud.py:43`
- Current mitigation: Documented inline as an intentional accommodation for a self-signed-cert deployment, with a comment recommending the operator flip it to `true` once behind a trusted cert.
- Recommendations: If the current Nextcloud endpoint has since moved behind Let's Encrypt/a trusted CA (common for a long-running VPS deployment), flip `NEXTCLOUD_VERIFY_TLS=true` in the live `.env`. At minimum, treat this default as MITM-exposed and confirm the current deployment's actual TLS posture before trusting resume upload confidentiality.

**Resume upload has no file-size guard (receipts do):**
- Risk: `handlers/registration.py::process_resume` validates only the file extension (`_is_allowed_resume` — `.pdf`/`.docx`) before accepting a `document.file_id` and later downloading+re-uploading it to Nextcloud. `handlers/payment.py` has an explicit `_RECEIPT_MAX_BYTES = 10 * 1024 * 1024` guard (`_receipt_too_large`) for the analogous receipt-upload path, but no equivalent constant/check exists for resumes.
- Files: `handlers/registration.py:1192` (`_is_allowed_resume`), `handlers/registration.py:1604` (`process_resume`), contrast with `handlers/payment.py:377-378`
- Current mitigation: None beyond Telegram's own ~20MB bot-download ceiling, which bounds worst-case impact but is not an intentional application-level control.
- Recommendations: Add the same `file_size` check used for receipts to `process_resume`, reusing (or extracting to a shared helper) the `_RECEIPT_MAX_BYTES`-style constant from `handlers/payment.py`.

**Google Sheets append failures are silent past the retry window:**
- Risk: `services/sheets.py::append_to_sheet` retries 3 times (5s/15s/30s backoff, ~50s total) then gives up with only a `logger.error(...)` call — no admin alert, no persistent retry queue, no flag on the SQLite row indicating the Sheets mirror is out of sync.
- Files: `services/sheets.py:115-133` (`append_to_sheet`), same pattern in `append_to_named_sheet` (`services/sheets.py:419-442`)
- Current mitigation: SQLite (`database/db.py`) is the source of truth and is unaffected — no data is lost, only the Sheets mirror silently drifts. `services/allowlist.py`'s empty-allowlist case has an explicit admin alert pattern (`allowlist_refresh_job`) that this path does not replicate.
- Recommendations: On final-attempt failure, send an admin alert (mirroring the `allowlist_refresh_job` pattern) so a prolonged Google API outage or quota exhaustion doesn't go unnoticed until a manager manually cross-checks the sheet against `/admin` stats.

**No rate limiting / abuse guard on `/start` re-entry or registration steps:**
- Risk: A single Telegram user can spam `/start` or resend the same registration step repeatedly with no per-user cooldown; each call triggers a `mark_reg_started` DB write and (once flow completes) a Sheets append + possible admin notification. There is no `aiogram` throttling middleware installed.
- Files: `handlers/registration.py::cmd_start`, `main.py` (no throttling middleware registered on the `Dispatcher`)
- Current mitigation: None at the framework level. Telegram's own global bot rate limits (30 msg/s) and the `allowlist`/subscription gate provide some friction but not per-user abuse throttling.
- Recommendations: Low priority at current scale (1000-1500 users, trusted AIESEC community audience, not a public open bot) — flag only if the bot's `/start` link is ever shared outside the intended audience.

## Performance Bottlenecks

**Sequential, one-message-at-a-time broadcasts:**
- Problem: All broadcast paths (`handlers/admin.py::process_broadcast`, `_wait_and_send_album`, `services/scheduler.py::send_scheduled_broadcast`, `sweep_payment_overdue`, `nudge_incomplete_registrations`) loop over target IDs and `await asyncio.sleep(0.05)` between sends — no batching, no concurrent fan-out.
- Files: `handlers/admin.py` (broadcast handlers ~line 1600-1690), `services/scheduler.py:218-225, 331-333, 381-385`
- Cause: Deliberate — Telegram Bot API has per-second send limits, and a naive concurrent fan-out would trigger `TelegramRetryAfter` storms. The `0.05s` spacing (~20 msg/s) is a safe-under-the-limit choice, not an oversight.
- Improvement path: At the documented 1000-1500 user scale, a full broadcast takes roughly 50-75 seconds sequentially — acceptable for the stated use case. If the user base grows materially (multi-thousand), consider a bounded-concurrency sender (e.g. `asyncio.Semaphore(20)` with per-chat backoff) rather than pure sequential sleep-based pacing.

**No SQL indexes beyond primary key + two explicit ones:**
- Problem: `database/db.py::init_db` creates only `idx_coins_user` (on `coins.user_id`) and `idx_consents_user` (on `user_consents.user_id`) via `CREATE INDEX IF NOT EXISTS`. Frequent filter columns used in moderation/broadcast queries — `users.status`, `users.payment_status`, `users.registration_date` — have no explicit index (only `telegram_id` as `PRIMARY KEY`).
- Files: `database/db.py` (`init_db`, `get_pending_users`, `count_and_list_filtered`, `get_receipt_pending_users`)
- Cause: Every `WHERE status = 'pending'` / `WHERE payment_status = 'receipt_sent'` scan is a full table scan.
- Improvement path: Not currently a real bottleneck — SQLite full-scans 1000-1500 rows in well under a millisecond. Add `CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)` and `idx_users_payment_status ON users(payment_status)` only if/when the user base grows an order of magnitude, or if moderation-queue queries are observed to slow down in production logs.

**`_get_allowlist_rows_sync` bypasses the cached Sheets client:**
- Problem: Unlike every other Sheets operation in `services/sheets.py` (which goes through the cached `_get_sheet()` handle guarded by `_sheet_lock`), `_get_allowlist_rows_sync` builds a brand-new `gspread.service_account(...)` client and re-opens the spreadsheet by key on every call.
- Files: `services/sheets.py:106-112`
- Cause: Likely written before the caching layer existed, or deliberately isolated to avoid coupling the allowlist tab lookup to the main-tab client cache.
- Improvement path: Minor — this only runs on the `allowlist_refresh_job` interval (default every 60 minutes per `services/scheduler.py`), so the extra ~3 round-trip auth cost is infrequent. Route it through `_get_sheet()`'s cached client (or a parallel cached handle for the allowlist tab) if Sheets API quota pressure is ever observed.

## Fragile Areas

**`handlers/admin.py` `pending_albums` module-global dict:**
- Files: `handlers/admin.py:64` (`pending_albums = {}`), `:1553-1620` (`_wait_and_send_album`, `process_broadcast`)
- Why fragile: Album-broadcast staging is tracked in an in-process dict keyed by `media_group_id`, drained by a background task that sleeps 0.8s then pops the entry. If the bot process restarts mid-collection (between the first album message arriving and the 0.8s drain), the pending entry is lost silently — no error surfaces to the admin, the broadcast simply never fires for that attempt.
- Safe modification: When touching the broadcast-album flow, preserve the pop-then-check-`None` guard in `_wait_and_send_album` (prevents a double-send if `_spawn` is somehow invoked twice for the same `media_group_id`). Do not add new global dicts for similar staging patterns without also handling the process-restart-mid-flow case, or without following the existing `services/background.py::spawn` strong-ref pattern to prevent silent GC of the drain task.
- Test coverage: No dedicated test file for the album-broadcast path found in `tests/` (broadcast tests focus on plain-text/photo broadcasts and 429 handling — `tests/test_broadcast_429_phase3.py` — not the album/`media_group_id` branch).

**`services/scheduler.py::send_scheduled_broadcast` forfeits the unsent tail on crash:**
- Files: `services/scheduler.py:188-230`
- Why fragile: The function atomically claims the broadcast row (`pending` → `sending`) before the send loop starts, explicitly by design (`ME-02` comment) to prevent a double-fire on restart. But if the process crashes partway through the send loop, the row is permanently stuck at `sending` — the remaining, un-notified recipients are never retried automatically; the code comment states this is intentional ("The unsent tail is forfeited by design; admin re-sends") but there is no admin-facing indicator that a broadcast is stuck in `sending` state versus genuinely mid-flight.
- Safe modification: If adding any admin-facing broadcast status view, surface `sending`-status rows older than a few minutes as a "possibly stuck — check delivery, resend manually" signal, since the current design has no automatic recovery path for this state.
- Test coverage: `tests/test_broadcast_429_phase3.py`, `tests/test_scheduler_helpers_phase3.py`, `tests/test_scheduler_reconcile_block6.py` cover reconciliation of dropped *pending* jobs and 429 retry classification, but not a mid-send-crash scenario (row stuck at `sending`).

**FSM state is entirely in-memory (`MemoryStorage`), and long registration flows have no server-side timeout:**
- Files: `main.py` (`Dispatcher(storage=MemoryStorage())`), `handlers/registration.py` (entire FSM step chain)
- Why fragile: A user who starts registration and abandons it mid-flow (closes Telegram, doesn't respond for days) leaves an FSM entry sitting in memory indefinitely (bounded only by process restarts, which clear it) — not a leak in the traditional sense since `MemoryStorage` is keyed per-chat and Telegram chat IDs are finite/reused, but there is no explicit expiry/cleanup logic for stale in-progress FSM sessions. The `reg_started` table (`database/db.py`) tracks the same abandonment for the dropout-nudge feature, so the *business* signal exists — the FSM memory itself just has no TTL.
- Safe modification: If ever migrating off `MemoryStorage` to a persistent FSM backend (e.g. for multi-instance scaling), audit for any handler logic that currently relies on FSM being wiped on restart as an implicit reset mechanism.
- Test coverage: `tests/test_dropout_lifecycle_block6.py` covers the `reg_started` dropout-tracking path; no test targets FSM memory growth/staleness directly (not practically testable without a long-running process).

## Scaling Limits

**Single SQLite file, no connection pool, per-call connections:**
- Current capacity: `database/db.py` opens a fresh `aiosqlite.connect(config.DB_PATH)` per function call, relying on SQLite's own file-level locking. Comfortable at the documented 1000-1500 users/season scale with a single bot process.
- Limit: SQLite's writer-serialization (one writer at a time) becomes a real bottleneck only under sustained concurrent write bursts (e.g. simultaneous mass-approve during peak registration windows) — not currently observed as an issue per the architecture docs, but a scaling ceiling if the event count/concurrency profile changes materially (e.g. running many concurrent events instead of one at a time, or 10x user scale).
- Scaling path: Add `PRAGMA journal_mode=WAL` if not already set (verify in `init_db`) to allow concurrent readers during writes; migrate to PostgreSQL only if the project outgrows single-process/single-event assumptions baked into `CLAUDE.md`'s "Fixed Core Stack" constraint (explicitly not recommended without an intentional re-scoping decision — see `INTEGRATIONS.md`/`STACK.md`).

**Single bot process, long polling, no horizontal scale-out:**
- Current capacity: Adequate for the stated single-event, 1000-1500-user scale; `MemoryStorage` FSM and in-RAM allowlist cache (`services/allowlist.py`) both assume exactly one process.
- Limit: Cannot run more than one bot instance against the same Telegram token (long polling + `getUpdates` conflicts across instances) without redesigning FSM storage (Redis-backed `RedisStorage`) and the allowlist cache (shared cache or DB-backed).
- Scaling path: Not needed at current scope; flag only if the bot is asked to serve multiple concurrent large events with materially higher throughput requirements.

## Dependencies at Risk

**`aiogram` unpinned upper bound (`>=3.0.0`):**
- Risk: No ceiling on the aiogram version pip will install; `CLAUDE.md`'s own stack lock explicitly treats aiogram 3.x as a hard constraint ("aiogram 3 + SQLite + long polling — сохранить, не переписывать"), meaning an eventual aiogram 4.x release is a breaking-change risk if a fresh install ever pulls it.
- Impact: Router registration API, FSM context API, or filter syntax changes in a hypothetical aiogram 4 would break most of `handlers/*.py` simultaneously.
- Migration plan: Add `aiogram>=3.0.0,<4.0.0` to `requirements.txt` now; no migration needed today, this is a preventive pin.

**APScheduler 4.0 alpha exists but is explicitly avoided:**
- Risk: None currently — `CLAUDE.md` documents this exact tradeoff and the codebase correctly pins `apscheduler==3.11.2`. Listed here only as a "watch" item: if APScheduler 4.0 reaches stable, `services/scheduler.py`'s `AsyncIOScheduler` + `SQLAlchemyJobStore` usage would need a deliberate, planned migration (breaking API) rather than an incidental version bump.
- Impact: N/A today.
- Migration plan: No action needed; re-evaluate only when APScheduler 4.0 ships a stable release and the project has a reason to move (do not upgrade opportunistically).

## Missing Critical Features

**No CI pipeline:**
- Problem: No GitHub Actions / other CI config detected anywhere in the repo (confirmed absent per `TESTING.md`). The 336-test suite (`tests/`) only runs when a developer manually invokes `pytest` locally.
- Blocks: Regressions can be merged without the test suite ever running; there is no automated gate on pull requests or pushes to `main`.

**No linting/formatting enforcement:**
- Problem: No `.flake8`, `ruff.toml`, `black` config, or pre-commit hooks detected (confirmed per `CONVENTIONS.md`).
- Blocks: Style drift across the 3000+-line handler files is caught only by code review, if at all.

## Test Coverage Gaps

**Broadcast album path (`media_group_id` staging):**
- What's not tested: `handlers/admin.py::_wait_and_send_album` and the `pending_albums` staging dict (see Fragile Areas above).
- Files: `handlers/admin.py:1553-1630`
- Risk: A regression in album-broadcast handling (e.g. a race between two admins sending overlapping media groups, or a malformed `media_group_id`) would not be caught by the existing test suite.
- Priority: Medium — album broadcasts are an infrequent admin action, but a silent failure here means a scheduled announcement never reaches participants with no error surfaced.

**`main.py` startup/shutdown wiring:**
- What's not tested: `main.py`'s `_configure_logging`, router-registration order, and graceful-shutdown sequence (scheduler stop, bot session close) have no dedicated test file.
- Files: `main.py`
- Risk: A router-registration-order regression (e.g. accidentally registering `registration.router` before `admin.router`, breaking the "admin commands intercepted first" invariant documented in `ARCHITECTURE.md`) would only be caught by manual smoke-testing, not the automated suite.
- Priority: Medium — router order is load-bearing (admin commands would get shadowed by registration FSM filters if reordered) but changes to `main.py` are infrequent.

**`keyboards/builders.py`:**
- What's not tested: No `tests/test_keyboards*.py` file exists; keyboard-visibility logic (which reads `bot_settings` to decide which buttons render) is exercised only indirectly through handler-level tests that happen to call a keyboard builder.
- Files: `keyboards/builders.py`
- Risk: A settings-driven button-visibility bug (e.g. a toggle that should hide "💳 Оплата" not being respected) could ship without a targeted test catching it.
- Priority: Low — the file is small (223 lines) and mostly declarative; risk is contained.

**Mid-send crash recovery for scheduled broadcasts:**
- What's not tested: The "stuck in `sending` status forever" scenario described under Fragile Areas (`services/scheduler.py::send_scheduled_broadcast`) has no test simulating a crash between `mark_broadcast_sending` and `mark_broadcast_sent`.
- Files: `services/scheduler.py:188-230`
- Risk: Any future refactor of this function could accidentally change the forfeit-the-tail behavior (e.g. introduce a double-send bug on retry) without a regression test catching it.
- Priority: Low — the current behavior is intentional and documented; a test would mainly guard against accidental regression, not an active bug.

---

*Concerns audit: 2026-07-24*
