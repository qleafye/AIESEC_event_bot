# Technology Stack

**Analysis Date:** 2026-07-24

## Languages

**Primary:**
- Python 3.11 - entire codebase (`main.py`, `handlers/`, `services/`, `database/`)
  - `.venv/pyvenv.cfg` pins `version = 3.11.9`
  - `Dockerfile` base image: `python:3.11-slim`

**Secondary:**
- SQL (SQLite dialect) - inline in `database/db.py` (raw `aiosqlite` queries, no ORM)

## Runtime

**Environment:**
- CPython 3.11, async/await throughout (`asyncio`)
- Windows dev machine uses `asyncio.WindowsSelectorEventLoopPolicy()` (`main.py:152-153`); production runs in Docker (Linux) via `Dockerfile`/`docker-compose.yml`

**Package Manager:**
- pip + `requirements.txt` (no lockfile/pinned hashes; only `apscheduler`, `sqlalchemy`, `gspread` have version constraints — everything else is unpinned `>=`/bare)
- No `pyproject.toml`, no Poetry/Pipenv

## Frameworks

**Core:**
- aiogram 3 (`aiogram>=3.0.0`, installed 3.24.0) - async Telegram Bot framework; long-polling (`dp.start_polling(bot)` in `main.py:134`), no webhooks
  - `Dispatcher(storage=MemoryStorage())` - FSM state is in-memory only, lost on restart (see CLAUDE.md constraint)
  - Routers: `handlers/admin.py`, `handlers/payment.py`, `handlers/registration.py`, `handlers/user_actions.py`, registered in that priority order in `main.py:119-122`
  - `aiohttp-socks` - enables `AiohttpSession(proxy=...)` SOCKS5/HTTP proxy support for regions where Telegram API is throttled (`main.py:97-100`, `PROXY_URL` in `.env`)

**Testing:**
- pytest (implied by `tests/test_*.py` naming and `.pytest_cache/` present; no explicit `pytest` pin in `requirements.txt` — installed separately/via dev env)
- No `pytest.ini`/`pyproject.toml` config file found — defaults used

**Build/Dev:**
- Docker (`Dockerfile`) + Docker Compose (`docker-compose.yml`) for production deployment
- No linter/formatter config found (no `.flake8`, `ruff.toml`, `.pylintrc`, `pyproject.toml` black/ruff sections)

## Key Dependencies

**Critical:**
- `aiosqlite` (latest, unpinned) - async SQLite driver, sole persistence layer for application data (`data/forum.db`)
- `pydantic-settings` (latest, unpinned) - typed `.env` config loading, `config.py`
- `apscheduler==3.11.2` - persistent job scheduling (delayed broadcasts, payment reminders, interval jobs) via `AsyncIOScheduler` + `SQLAlchemyJobStore`
- `sqlalchemy>=2.0,<3.0` - required only as APScheduler's job-store backend (sync engine against a *separate* sqlite file `data/jobs.sqlite`); not used for application queries
- `gspread>=6.0,<7.0` (installed 6.2.1) + `google-auth` - Google Sheets integration (registration export, allowlist, status sync)

**Infrastructure:**
- `python-dotenv` - `.env` loading (used together with pydantic-settings' `env_file` support)
- `aiohttp` (transitive via aiogram) - also used directly in `services/nextcloud.py` for WebDAV PUT requests

## Configuration

**Environment:**
- All runtime config loaded via `config.py` → `Settings(BaseSettings)` (pydantic-settings), reads `.env` at repo root (`model_config = SettingsConfigDict(env_file=".env", ..., extra="ignore")`)
- `.env.example` documents every variable in Russian with setup instructions (Nextcloud network topology notes, Google Sheet ID, etc.)
- Key settings: `BOT_TOKEN` (SecretStr), `ADMIN_IDS` (JSON list of ints), `PROXY_URL` (SecretStr, optional), `UNIVERSITIES` (JSON list, has a Russian-university default), `DB_PATH` (default `data/forum.db`), `LOG_LEVEL`
- Google Sheets: `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_FILE` (default `google_credentials.json`), `GOOGLE_SHEET_TAB`
- Nextcloud (optional, fail-soft when unset): `NEXTCLOUD_WEBDAV_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASS` (SecretStr), `NEXTCLOUD_FOLDER`, `NEXTCLOUD_PUBLIC_URL`, `NEXTCLOUD_FOLDER_SHARE_TOKEN`, `NEXTCLOUD_VERIFY_TLS`
- Runtime, DB-backed settings (not `.env`): `bot_settings` key/value table (`database/db.py:97`) holds admin-tunable values (nudge timing, payment texts, feature toggles) read via `get_setting()`/`set_setting()` — this is a second config layer on top of `.env`, editable live through admin handlers without a restart
- A hidden third channel exists: `google_credentials.json` (service-account key, tracked as present on disk but must never be read/quoted)

**Build:**
- `Dockerfile`: single-stage, `pip install -r requirements.txt`, `COPY . .`, `CMD ["python", "main.py"]`
- `docker-compose.yml`: mounts `./data`, `./logs`, `./google_credentials.json` (ro), `./resources` (ro); pins DNS to `8.8.8.8`/`8.8.4.4`; `restart: always`; JSON-file log driver capped at 10m×3 files

## Platform Requirements

**Development:**
- Python 3.11 (matches `.venv`); Windows dev environment uses `WindowsSelectorEventLoopPolicy` explicitly to avoid asyncio/Proactor issues on Windows
- Local SQLite files under `data/` (gitignored)

**Production:**
- Docker container (`python:3.11-slim`), long-running process (no webhook server, no exposed ports)
- Two SQLite files: `data/forum.db` (application data, `aiosqlite`) and `data/jobs.sqlite` (APScheduler job store, `sqlalchemy`) — kept intentionally separate (see `services/scheduler.py` header comment: "never forum.db")
- Rotating file logs at `logs/bot.log` (10MB × 5 backups); stdout limited to WARNING+ for clean `docker logs`
- Optional self-hosted Nextcloud instance reachable via WebDAV for resume storage (same-host Docker network preferred per `.env.example` notes)
- Optional SOCKS5/HTTP proxy in front of Telegram Bot API for network-restricted regions

---

*Stack analysis: 2026-07-24*
