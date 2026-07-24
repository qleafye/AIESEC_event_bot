# Coding Conventions

**Analysis Date:** 2026-07-24

## Naming Patterns

**Files:**
- `snake_case.py` throughout — `database/db.py`, `handlers/registration.py`, `services/nextcloud.py`, `keyboards/builders.py`
- One module per Telegram feature area under `handlers/` (`admin.py`, `payment.py`, `registration.py`, `user_actions.py`, `states.py`); one module per infra concern under `services/` (`scheduler.py`, `sheets.py`, `nextcloud.py`, `allowlist.py`, `reminders.py`, `background.py`)
- Test files: `tests/test_<feature>_<phase-or-block-tag>.py`, e.g. `tests/test_registration_phase1.py`, `tests/test_admin_phase5.py`, `tests/test_block7_low.py`, `tests/test_scheduler_reconcile_block6.py`. The suffix ties the file to the planning phase/block that introduced it — new tests for a phase's work go in a new `_phaseN`/`_blockN` file rather than appended to an unrelated existing file, unless directly extending that phase's feature.

**Functions:**
- `snake_case()` everywhere, no exceptions found.
- **Private/internal helpers prefixed with a single underscore**: `_decide_status`, `_build_summary`, `_is_allowed_resume`, `_ask_step`, `_parse_appr`, `_render_application_card`, `_csv_safe`, `_ensure_column`, `_assert_identifier`. This is the primary signal of "not part of the module's public/handler surface" — pure helpers extracted specifically to be unit-testable without Telegram/DB fixtures (see TESTING.md).
- aiogram handler functions registered via `@router.message(...)` / `@router.callback_query(...)` decorators use descriptive verb-first or state-first names (`start_registration`, `process_age`, `appr_all_yes`, `appr_reject_cancel`) — not prefixed with underscore since they're the public router surface.
- Keyboard builders are named `get_<thing>_kb()`, always returning a `ReplyKeyboardMarkup` or `InlineKeyboardMarkup` — `get_main_menu_kb`, `get_cancel_kb`, `get_phone_kb`, `get_source_kb`. See `keyboards/builders.py`.

**Variables:**
- `snake_case`, short and local (`kb`, `db`, `p` for progress-prefix string, `uid`).
- Module-level constants are `UPPER_SNAKE_CASE`: `DEFAULT_START_TEXT`, `REG_FLOW`, `MENU_BUTTONS`, `APPROVAL_SETTINGS_DOC`, `PARTY_SHEET_TAB_DEFAULT`.
- Callback-data strings use a short prefix + verb pattern: `appr_approve:123`, `appr_all_yes`, `appr_reject_cancel`, `menu_referral`. Parsed back out with small `_parse_*` helpers (e.g. `_parse_appr` in `handlers/admin.py`) rather than manual string-splitting inline at each call site.

**Types:**
- No dataclasses/Pydantic models for domain entities — users, coins, settings are plain `dict`/`sqlite3.Row`-like mappings returned from `database/db.py` async functions (e.g. `get_user()` returns a dict-like row).
- `pydantic_settings.BaseSettings` is used exactly once, for `config.Settings` in `config.py` — the one place structured config validation is warranted.
- Type hints are used selectively, mainly on function signatures for pure helpers and where `str | None` union syntax communicates optionality, e.g. `def _decide_status(reg_mode: str, full_setting: str, short_setting: str, participant_type: str = "full", party_setting: str | None = None) -> str:` (`handlers/registration.py`). Not universally applied — many handler functions and most `services/`/`database/` async functions have no annotations at all. Follow the file you're editing: add hints on new pure helpers, don't retrofit unrelated handlers.

## Code Style

**Formatting:**
- No formatter config found (no `.prettierrc`, no `black`/`ruff` config, no `pyproject.toml`). Formatting is whatever the author wrote — 4-space indentation, no enforced line-length limit (lines run long, especially import lists and f-string chains in `handlers/registration.py` and `handlers/admin.py`).

**Linting:**
- No linter config found (no `.flake8`, `ruff.toml`, `setup.cfg`). Do not assume any linter will run in CI — there is no CI config detected either. Match surrounding style by eye.

## Import Organization

**Order (as observed, e.g. `handlers/registration.py` top of file):**
1. Standard library (`asyncio`, `json`, `logging`, `html`, `datetime`, `os`)
2. Third-party (`aiogram` submodules)
3. Local application imports (`config`, `database.db`, `handlers.states`, `keyboards.builders`, `services.*`)

Within the local group, `database.db` functions are frequently imported by name in a long explicit list rather than `import database.db as db`:
```python
from database.db import add_user, get_user, get_setting, set_setting, mark_reg_started, clear_reg_started, set_reg_step, set_user_subscribed, set_user_status, record_user_consent, get_user_consents, get_reg_started_track, _csv_safe
```
Follow this pattern for new `database.db` functions used in a handler — add the name to the existing import line rather than introducing `db.function_name()` call style in a file that already uses named imports.

**Path Aliases:**
- None. All imports are plain package-relative from repo root (`from database.db import ...`, `from services.sheets import ...`, `from handlers.registration import ...`). No `src/` layout, no `sys.path` manipulation beyond what's implicit in running from repo root.

**Circular import avoidance:**
- Lazy (function-local) imports are used deliberately to break cycles — e.g. `keyboards/builders.py` does `from handlers.payment import should_offer_receipt_upload` inside `get_main_menu_kb()` because `handlers.payment` imports `get_main_menu_kb` at module level. When adding a new cross-module dependency, check for an existing top-level import in the reverse direction first; if found, import lazily inside the function instead of at module top.

## Error Handling

**Patterns:**
- `try/except Exception` is the dominant idiom for anything that touches Telegram API, Google Sheets, Nextcloud, or scheduler I/O — treated as fail-soft boundaries so one flaky external call never crashes the bot or blocks a user flow. Example (`handlers/registration.py`):
```python
try:
    await set_reg_step(message.chat.id, step_key)
except Exception as e:
    logger.error(f"set_reg_step failed for {message.chat.id} @ {step_key}: {e}")
```
- Bare `except Exception:` (no `as e`, no logging) is used where the fallback path itself is the recovery — e.g. multi-source photo-send retries in `handlers/registration.py` (~line 1280) try several `file_id`/URL sources in a loop and silently continue to the next.
- A **global aiogram error handler** in `main.py` (`@dp.errors()` → `_on_update_error`) is the last-resort net: logs the exception with `exc_info` plus the offending update JSON, then returns `True` so aiogram doesn't propagate further. This means individual handlers do NOT need to wrap every line in `try/except` defensively — only I/O calls to external services (Sheets, Nextcloud, Telegram file operations) that have known fail-soft semantics.
- Custom validation errors use `ValueError` with a descriptive message, e.g. `_assert_identifier` in `database/db.py`:
```python
def _assert_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name
```
- Shutdown/cleanup code in `main.py` wraps each independent cleanup step (scheduler shutdown, bot session close) in its own `try/except Exception` so one failing cleanup doesn't block the other.

## Logging

**Framework:** stdlib `logging`, module-level logger via `logger = logging.getLogger(__name__)` at the top of every handler/service file that logs.

**Configuration (`main.py::_configure_logging`):**
- Dual-handler setup: `RotatingFileHandler` (`logs/bot.log`, 10MB × 5 backups) at `config.LOG_LEVEL` (default `INFO`), plus a `StreamHandler(sys.stdout)` pinned to `WARNING+` so `docker logs` stays clean while the file captures full detail.
- Third-party loggers (`aiogram.event`, `apscheduler`, `urllib3`, `gspread`, `asyncio`) are explicitly silenced to `WARNING` to cut framework noise.

**Patterns:**
- `logger.error(f"...")` with f-string interpolation is more common than `%s`-style lazy formatting, though both appear (`main.py` uses `logger.error("Unhandled update error: %s", event.exception, exc_info=event.exception)` for the one place stack traces matter).
- `exc_info=True` or `exc_info=event.exception` is used when the exception itself is informative and worth a full traceback (startup failures, the global error handler); omitted for expected fail-soft branches where the message alone is enough.
- Comments frequently justify *why* a given block logs-and-continues rather than raises — follow this pattern: a one-line comment above a fail-soft `except` explaining the tradeoff (e.g. "Fail-soft — never blocks startup").

## Comments

**When to Comment:**
- Comments are used heavily to record **decision provenance** — tagged with a short ID referencing a planning phase/finding, e.g. `# WR-08:`, `# CR-9:`, `# IN-02:`, `# D-13:`, `# Phase 5 (D-11, plan 05-06):`. These IDs trace back to `.planning/` phase docs or audit findings. When making a non-obvious behavioral choice (especially fixing a bug or resolving an edge case), add a similar short comment explaining *why*, not just *what* — this codebase treats comments as an audit trail, not just clarification.
- Long explanatory comments precede tricky logic (SQL identifier injection guard in `database/db.py`, party-track status resolution in `_decide_status`, welcome-drain ordering in `handlers/admin.py`). Prefer a multi-line comment block over a docstring when explaining *why a specific ordering/edge-case matters*, and a docstring when explaining *what a function does*.
- Inline Russian-language comments appear alongside English ones, especially for domain/business-logic nuance aimed at the non-technical stakeholder ("Tatiana") who requested the behavior, e.g. `# Tatiana: «поздравляем» теперь приходит СРАЗУ после регистрации...`. Mixing languages in comments is normal in this codebase; keep domain-specific asides in Russian if quoting a stakeholder requirement, keep structural/technical explanation in English or Russian per surrounding file.

**Docstrings:**
- Triple-quoted docstrings are used on most non-trivial functions and at the top of test files, written in plain English, often multi-paragraph, explaining rationale and edge cases rather than just parameters (no Google/NumPy-style `Args:`/`Returns:` sections observed). Example (`handlers/registration.py::_decide_status`):
```python
def _decide_status(reg_mode: str, full_setting: str, short_setting: str,
                    participant_type: str = "full", party_setting: str | None = None) -> str:
    """Form type x per-form moderation setting -> 'pending' | 'approved'.
    Full form uses full_setting, short form uses short_setting; 'manual' -> pending.

    Phase 5 (D-13): party tracks resolve status from party_approval alone, completely
    independent of full_approval/short_approval — this branch never falls through to the
    reg_mode logic below it..."""
```
- Test-file module docstrings state which phase/finding the file covers and any environment caveat (e.g. "pytest-asyncio is unavailable in this env, so each test drives the async db helpers via asyncio.run()").

## Function Design

**Size:** Handler files (`handlers/admin.py` at 3120 lines, `handlers/registration.py` at 2446 lines) are large, organized as flat sequences of router-decorated handlers plus a block of `_private` pure helpers near related handlers. No enforced function-length limit; individual handler functions can run 50-150+ lines when they orchestrate a multi-step flow (state transition + Sheets sync + notification). When a piece of logic is independently testable (parsing, formatting, decision logic), it is deliberately extracted into a small `_private` function — this is the dominant refactoring pattern in this codebase, not splitting into new files/classes.

**Parameters:** Plain positional/keyword parameters with defaults; no `*args`/`**kwargs` catch-alls in domain code (kept only in the pattern `async def edit_text(self, *a, **k)` for test fakes/stubs mimicking aiogram's `Message`/`CallbackQuery` interface).

**Return Values:** Async DB functions return plain `dict`, `list[dict]`, primitive, or `None` (never a custom result/error wrapper type) — callers check `is None`/truthiness/`Exception` rather than a `Result`-style type. Pure helper functions used for formatting return `str` (HTML-escaped for Telegram `ParseMode.HTML`, via `html.escape()`).

## Module Design

**Exports:** No `__all__` declarations found; all public names are import-by-name. `handlers/__init__.py`, `database/__init__.py`, `keyboards/__init__.py` are empty — no barrel re-exports, callers import directly from the submodule (`from database.db import ...`, `from handlers.admin import ...`).

**Barrel Files:** Not used. `__init__.py` files are empty placeholders only, confirming the "import from the concrete submodule" convention above.

**Router registration:** Each `handlers/*.py` module defines its own `router = Router()` at module scope; `main.py` registers them explicitly in a fixed order via `dp.include_router(...)` — order matters (comment: `# Admin first to intercept commands`, `# payment callbacks/states checked before registration`). When adding a new handler module, add its router include call to `main.py` in the position that respects existing interception priority (admin > payment > registration > user_actions).

---

*Convention analysis: 2026-07-24*
