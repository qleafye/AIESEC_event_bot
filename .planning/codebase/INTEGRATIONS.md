# External Integrations

**Analysis Date:** 2026-07-24

## APIs & External Services

**Telegram Bot API:**
- Core interaction surface for the entire bot. Long polling only (`dp.start_polling(bot)`, `main.py:134`); webhook explicitly dropped on boot (`bot.delete_webhook(drop_pending_updates=True)`, `main.py:124`)
- SDK/Client: `aiogram` 3 (`Bot`, `Dispatcher`)
- Auth: `BOT_TOKEN` (SecretStr, from `.env`, BotFather-issued)
- Optional proxy in front of the API: `PROXY_URL` (SecretStr) → `AiohttpSession(proxy=...)`, needs `aiohttp-socks` installed (`main.py:97-100`)
- `getChatMember` used for observe-only channel-subscription check: `handlers/registration.py:1334-1341` (`is_subscribed`), fail-open on any error (bot not admin, unknown channel, etc.)
- File transfer via `file_id` pattern: resumes (`resume_file_id`) and payment receipts (`receipt_file_id`) are stored as Telegram `file_id` strings in SQLite, never downloaded to local disk except transiently when re-uploading to Nextcloud (`bot.download(file_id)` in `services/nextcloud.py:87`)
- Rate-limit handling: `TelegramRetryAfter` caught and retried once with `sleep(retry_after + 1)` in `services/scheduler.py:169-185` (`_safe_send`) for scheduled broadcasts, payment reminders, dropout nudges, overdue notices

**Google Sheets API:**
- Category: spreadsheet-backed data export / secondary source of truth for admins
- Used for: registration row export (main data tab), pre-selection allowlist tab (read-only), "Незавершённые" (incomplete registrations) tab, optional party-track tab, per-row status sync with dropdown + conditional formatting
- SDK/Client: `gspread` (6.2.1) + `google-auth` (service account flow, `gspread.service_account(filename=...)`)
- Auth: `GOOGLE_CREDENTIALS_FILE` (default `google_credentials.json`, a service-account key file present at repo root — never read/quoted by tooling)
- Config: `GOOGLE_SHEET_ID` (spreadsheet id), `GOOGLE_SHEET_TAB` (target tab name; empty = `.sheet1` legacy default)
- Implementation: `services/sheets.py` (472 lines) — all Sheets calls are synchronous `gspread` calls wrapped in `asyncio.to_thread(...)`, with a thread-safe double-checked-lock cache (`_sheet`/`_sheet_lock`, and a parallel `_named_sheets` cache for arbitrary tabs)
- Fail-soft everywhere: every public function checks `GOOGLE_SHEET_ID`/`GOOGLE_CREDENTIALS_FILE` are set and wraps calls in try/except, returning `None`/`-1`/`False` on failure — a Sheets outage never blocks registration or bot startup
- Retry policy: `MAX_RETRIES = 3`, `RETRY_DELAYS = [5, 15, 30]` seconds for row appends (`append_to_sheet`, `append_to_named_sheet`)
- Consumers: `handlers/registration.py` (append on finalize), `handlers/admin.py` (bulk sync/rebuild/dedupe/export triggers), `services/scheduler.py` (`sync_incomplete_sheet_job`, allowlist refresh), `main.py` (header sync at startup)

**Nextcloud (self-hosted, WebDAV):**
- Category: optional cloud file storage for resumes
- Used for: uploading resume files/text as an alternative to keeping only the Telegram `file_id`, producing a shareable deep-link written to DB (`resume_url`) and the Sheets export
- SDK/Client: raw `aiohttp` HTTP calls (WebDAV `PUT`), no dedicated SDK — `services/nextcloud.py` (124 lines)
- Auth: HTTP Basic (`NEXTCLOUD_USER` + `NEXTCLOUD_APP_PASS` app-password, not the account password)
- Config: `NEXTCLOUD_WEBDAV_URL` (full per-user DAV endpoint, must end in `/remote.php/dav/files/<user>/`), `NEXTCLOUD_FOLDER` (default `resumes`), `NEXTCLOUD_PUBLIC_URL`, `NEXTCLOUD_FOLDER_SHARE_TOKEN` (one manually-created public folder share, not per-file OCS shares), `NEXTCLOUD_VERIFY_TLS` (default `false` — self-signed cert deployments)
- Sharing model: single manually-created public folder share; bot only PUTs bytes and builds a deep-link `{PUBLIC_URL}/s/{FOLDER_SHARE_TOKEN}/download?path=%2F&files=<name>` — no password is ever generated, logged, or stored by the bot (set once by hand in Nextcloud UI)
- Fully fail-soft: `upload_resume()`/`upload_text_resume()` catch all exceptions and return `None`; feature is off entirely when any of `NEXTCLOUD_WEBDAV_URL`/`NEXTCLOUD_PUBLIC_URL`/`NEXTCLOUD_FOLDER_SHARE_TOKEN` is empty
- 15-second timeout on all WebDAV calls (`aiohttp.ClientTimeout(total=15)`)

## Data Storage

**Databases:**
- SQLite (application data): `data/forum.db`, path from `config.DB_PATH` (default). Accessed exclusively via `aiosqlite` (async), raw SQL, no ORM. Schema/migrations live entirely in `database/db.py` (`init_db()`, `_ensure_column()` additive-migration helper, `CREATE TABLE IF NOT EXISTS`)
  - Tables: `users` (core registrant record, ~40+ columns accreted via `_ensure_column`), `bot_settings` (key/value admin-tunable config), `coins` (append-only gamification ledger — balance = `SUM(delta)`, never `UPDATE`), `reg_started` (dropout tracking, independent of in-memory FSM), `scheduled_broadcasts` (payload store for APScheduler date jobs), `user_consents` (per-user consent audit trail, `UNIQUE(user_id, consent_key)`)
- SQLite (scheduler job store): `data/jobs.sqlite`, accessed via `sqlalchemy` (sync engine, `sqlite:///data/jobs.sqlite`) exclusively by `APScheduler`'s `SQLAlchemyJobStore` (`services/scheduler.py:26,85`). Deliberately kept separate from `forum.db` per an explicit "Pitfall 2" comment in `services/scheduler.py`.

**File Storage:**
- Primary: Telegram servers via `file_id` (permanent references stored as TEXT columns — `resume_file_id`, `receipt_file_id`) — no local disk storage of uploaded content
- Secondary (optional): self-hosted Nextcloud via WebDAV (see above), for resumes only

**Caching:**
- In-process RAM caches only, no external cache service (no Redis/Memcached)
  - `services/allowlist.py`: module-global `_allowlist: set[str]`, refreshed from a Google Sheet tab at startup, on an interval job, and on admin trigger — `/start` never calls gspread directly, only checks this RAM set
  - `services/sheets.py`: `_sheet` / `_named_sheets` — cached `gspread` worksheet handles (avoids re-auth per call)

## Authentication & Identity

**Auth Provider:**
- No external auth provider (no OAuth/SSO/Firebase Auth). Identity = Telegram `user_id`/`username` as provided by the Telegram Bot API on each update
- Admin authorization: static allowlist via `ADMIN_IDS` (JSON list of Telegram numeric ids) in `.env`, checked in `handlers/admin.py`
- Optional pre-selection gate: username allowlist sourced from a Google Sheet tab (see `services/allowlist.py`), fail-open (`preselect_enabled` off → everyone allowed; allowlist empty while gating on → loud admin alert but still fail-open by design)

## Monitoring & Observability

**Error Tracking:**
- None (no Sentry/Bugsnag/etc.). All errors funnel to the standard `logging` module
- Global aiogram error handler (`@dp.errors()` in `main.py:108-115`) logs unhandled update exceptions plus the offending update JSON, then returns `True` (handled) so updates are never silently dropped

**Logs:**
- Python stdlib `logging`, configured in `main.py:_configure_logging()`
- Dual-handler setup: `RotatingFileHandler` at `logs/bot.log` (10MB × 5 backups, full detail per `LOG_LEVEL`) + `StreamHandler(sys.stdout)` pinned to WARNING+ (keeps `docker logs` output clean)
- Noisy third-party loggers (`aiogram.event`, `apscheduler`, `urllib3`, `gspread`, `asyncio`) forced to WARNING
- PII discipline: several log call sites explicitly log only an id, never full row content (e.g. `services/sheets.py:125` comment "WR-06: log only the id, not the full row — data carries PII")

## CI/CD & Deployment

**Hosting:**
- Self-managed Docker host (VPS), not a managed platform (no Heroku/Vercel/Railway config found)
- `docker-compose.yml` targets a single `bot` service with `restart: always`, pinned DNS resolvers, and read-only bind mounts for credentials/resources

**CI Pipeline:**
- None found (no `.github/workflows/`, no GitLab CI, no Azure Pipelines config)

## Environment Configuration

**Required env vars:**
- `BOT_TOKEN` (required, no default)
- `ADMIN_IDS` (required, no default)

**Effectively-required for full functionality (feature flags via absence):**
- `GOOGLE_SHEET_ID` + `GOOGLE_CREDENTIALS_FILE` — Sheets export/allowlist/status-sync (all fail-soft-off when unset)
- `NEXTCLOUD_WEBDAV_URL` + `NEXTCLOUD_PUBLIC_URL` + `NEXTCLOUD_FOLDER_SHARE_TOKEN` — resume cloud upload (fail-soft-off when unset)
- `PROXY_URL` — only needed where direct Telegram API access is throttled

**Secrets location:**
- `.env` at repo root (gitignored via `.gitignore`), loaded by `pydantic-settings`
- `google_credentials.json` at repo root (gitignored), Google service-account key
- Secrets are typed `SecretStr` in `config.py` (`BOT_TOKEN`, `PROXY_URL`, `NEXTCLOUD_APP_PASS`, `NEXTCLOUD_SHARE_PASSWORD`) so accidental `str(config)`/logging does not leak raw values — call sites must explicitly use `.get_secret_value()`

## Webhooks & Callbacks

**Incoming:**
- None. Telegram updates arrive via long polling only; no HTTP server is run by the bot, no incoming webhooks from any third party

**Outgoing:**
- None (no outbound webhook calls to third-party systems). All "outgoing" integration traffic is either Telegram Bot API calls, Google Sheets API calls, or Nextcloud WebDAV PUTs — all described above, none of which are webhook/callback-style

---

*Integration audit: 2026-07-24*
