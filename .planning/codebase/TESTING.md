# Testing Patterns

**Analysis Date:** 2026-07-24

## Test Framework

**Runner:**
- `pytest` (installed in `.venv`; not pinned in `requirements.txt` — it's a dev-only dependency, add it to a separate dev-requirements file or install manually if setting up a fresh environment). No `pytest.ini`, `pyproject.toml`, `setup.cfg`, or `conftest.py` exists anywhere in the repo — pytest runs on defaults (auto-discovery of `tests/test_*.py`, `test_*` functions).

**Assertion Library:**
- Plain `assert` statements only (pytest's assertion rewriting). No `unittest.TestCase`, no third-party assertion library (no `pytest-mock`, no `hamcrest`).

**Run Commands:**
```bash
python -m pytest                          # Run all tests (from repo root)
python -m pytest tests/test_db_phase1.py  # Run a single file
python -m pytest -k test_balance_is_sum   # Run tests matching a name pattern
python -m pytest -v                       # Verbose per-test output
```
No coverage tool config detected (no `pytest-cov`/`.coveragerc`) — coverage is not currently measured.

**Critical environment constraint:** `pytest-asyncio` is **not installed/available** in this environment. This is explicitly documented in test-file docstrings (e.g. `tests/test_db_phase1.py`: *"pytest-asyncio is unavailable in this env, so each test drives the async db helpers via asyncio.run()..."*). Every async call in a test is driven manually via `asyncio.run(...)` inside an ordinary (synchronous) `def test_...():` function — there are **zero** `async def test_...` functions in the entire suite (verified: `grep -rln "async def test_" tests/*.py` → 0 matches). When writing new tests, do NOT reach for `@pytest.mark.asyncio` or an `async def test_` signature — it will not run. Always wrap async calls with `asyncio.run(...)`.

## Test File Organization

**Location:**
- Flat `tests/` directory at repo root, no subdirectories, no test package `__init__.py`. Not co-located with source.

**Naming:**
- `tests/test_<feature>_<phase-or-block-tag>.py` — the tag (`phase1`, `phase2`, `phase3`, `phase4`, `phase5`, `blockN`, `c0x`, etc.) identifies the planning phase/quick-task that introduced the tested behavior, mirroring `.planning/phases/` naming. New tests for work on an existing phase's feature area should extend that phase's file; genuinely new work gets a new file named after the current phase/block tag, not appended to an unrelated file.
- Test function names read as a sentence describing the behavior under test: `test_migration_preserves_existing_users_with_approved_status`, `test_negative_balance_allowed`, `test_wr01_welcome_drain_scheduled_despite_edit_failure`. Prefixing with an internal finding ID (`wr01`, `qw03`) when the test exists specifically to pin down a fix for that finding is common — trace it back to `.planning/` if the ID isn't self-explanatory.

**Structure:**
```
tests/
├── test_admin_phase2.py
├── test_admin_phase5.py
├── test_allowlist_phase3.py
├── test_alumni_status.py
├── test_backfill_resumes.py
├── test_background_spawn.py
├── test_block7_low.py
├── test_broadcast_429_phase3.py
├── test_coins_phase1.py
├── test_csv_injection.py
├── test_db_phase1.py / phase2 / phase4 / phase5
├── test_dropout_lifecycle_block6.py
├── test_filters_phase3.py
├── test_nextcloud_wk6.py
├── test_nudge_phase3.py
├── test_party_header_block6.py
├── test_payment_lc_requisites.py / test_payment_phase5.py
├── test_receipt_counter_block6.py
├── test_registration_phase1.py / phase2 / phase4 / phase5
├── test_reminders_phase2.py
├── test_review_b1b.py
├── test_scheduler_helpers_phase3.py / test_scheduler_reconcile_block6.py
├── test_settings_groups_c0x.py
├── test_sheets_phase5.py
└── test_subscription_phase1.py
```
33 test files, 336 test functions total (as of this analysis).

## Test Structure

**Suite Organization:**
Tests are grouped within a file using `# ── Section Name ──...` comment banners (an em-dash rule), not classes:
```python
# ── Migrations ───────────────────────────────────────────────────────────────

def test_migration_preserves_existing_users_with_approved_status(tmp_path):
    ...

# ── Coins ledger ─────────────────────────────────────────────────────────────

def test_balance_is_sum_of_deltas(tmp_path):
    ...
```
No test classes (`class Test...`) are used anywhere — flat functions grouped by comment banner only.

**Patterns:**
- **DB isolation via `tmp_path` fixture**: every DB-touching test calls a local `_use_tmp_db(tmp_path)` helper (redefined per file, not shared via `conftest.py`) that points `config.DB_PATH` at a fresh file in pytest's `tmp_path`, then calls `asyncio.run(db.init_db())`. Because `config.DB_PATH` is a module-level mutable setting, this pattern also implicitly provides test isolation — but tests are NOT safe to run in parallel/random order across files if `conftest.py`-level isolation isn't added, since `config.DB_PATH` is a shared global. Follow the exact existing helper shape for new DB tests:
```python
def _use_tmp_db(tmp_path):
    db_file = tmp_path / "test_forum.db"
    config.DB_PATH = str(db_file)
    return db_file
```
- **Async driving**: `asyncio.run(db.some_async_fn(...))` per call, never batched into a single `asyncio.run(main())` unless multiple awaits must share the same event loop context (e.g. background-task tests that need `asyncio.all_tasks()` to see spawned tasks — see WR-01 test in `tests/test_admin_phase2.py`, which wraps the handler call plus a drain of `asyncio.all_tasks()` inside one `async def go(): ...; asyncio.run(go())`).
- **Setup/teardown**: no `setUp`/`tearDown`, no `conftest.py` fixtures beyond pytest's built-in `tmp_path`. Each test is self-contained: create tmp DB → init → seed → call → assert, all inline.

## Mocking

**Framework:** pytest's built-in `monkeypatch` fixture is the dominant mocking tool (`monkeypatch.setattr(config, "ADMIN_IDS", [uid])`, `monkeypatch.setattr(admin, "approve_all_pending", fake_approve_all_pending)`). `unittest.mock` (`AsyncMock`/`MagicMock`) appears in a minority of files for heavier external-service mocking. Used in 9 of 33 test files (`test_admin_phase2.py`, `test_admin_phase5.py`, `test_block7_low.py`, `test_party_header_block6.py`, `test_payment_phase5.py`, `test_registration_phase5.py`, `test_scheduler_reconcile_block6.py`, `test_sheets_phase5.py`, `test_subscription_phase1.py`).

**Patterns:**

1. **`monkeypatch.setattr` to stub a module-level async function** (preferred for handler-level tests that need to isolate one function from its collaborators):
```python
async def fake_approve_all_pending():
    return [1, 2, 3]

async def fake_welcome_flipped(bot, ids):
    welcomed.append(list(ids))

monkeypatch.setattr(admin, "approve_all_pending", fake_approve_all_pending)
monkeypatch.setattr(admin, "_welcome_flipped", fake_welcome_flipped)
```
Patch on the **importing module's namespace** (`admin`, not `database.db`) — matches how aiogram handler modules import their dependencies by name.

2. **Hand-rolled fake objects for aiogram `Message`/`CallbackQuery`** rather than mocking the real aiogram classes — small classes implementing just the attributes/methods the handler under test touches:
```python
class _RaisingMessage:
    async def edit_text(self, *a, **k):
        raise RuntimeError("Bad Request: message can't be edited (too old)")

class _FakeCallbackWR01:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.message = _RaisingMessage()
        self.bot = object()
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True
```
This is the dominant pattern for testing router-decorated handler functions directly (bypassing aiogram's dispatch machinery entirely — the handler function is called as a plain coroutine with a fake `callback`/`message` object). Prefer this over trying to construct real `aiogram.types.CallbackQuery` instances, which require full nested Telegram API payloads.

3. **`types.SimpleNamespace` for minimal ad-hoc stand-ins** where only one or two attributes matter:
```python
import types as _types
def m(t):
    return _types.SimpleNamespace(text=t)
```

**What to Mock:**
- External I/O boundaries: Telegram API calls (`bot.send_message`, `message.edit_text`), Google Sheets sync (`bulk_update_status_in_sheet`), scheduler/background task spawning, Nextcloud upload.
- Sibling handler-module functions the function-under-test calls but that aren't the subject of the test (e.g. `_show_current_card`, `build_admin_keyboard`).

**What NOT to Mock:**
- The SQLite database layer for DB-behavior tests — use a real `tmp_path`-backed SQLite file via `db.init_db()` rather than mocking `aiosqlite`. This is deliberate: migration/schema tests (`test_db_phase1.py`) need real SQLite behavior (e.g. `PRAGMA table_info`, `ALTER TABLE`) to be meaningful.
- Pure helper functions under test (`_decide_status`, `_build_summary`, `_csv_safe`, `_is_allowed_resume`, `_retry_delay`, `_classify_outcome`) — these are called directly, unmocked, with plain inputs/outputs asserted. This is the largest category of test in the suite.

## Fixtures and Factories

**Test Data:**
- No shared fixture/factory module. Test data is constructed inline per test as plain dicts matching the DB row shape, e.g.:
```python
asyncio.run(db.add_user({
    "telegram_id": 222,
    "full_name": "First Name",
    "registration_date": "2025-02-01",
}))
```
- No `faker`/factory library. IDs and names are hand-picked literals, often incrementing per test within a file to avoid collisions in the shared tmp DB (e.g. `333`, `444`, `555`, `666`, `777` across sequential tests in `test_registration_phase1.py`) — follow this convention (bump the literal ID) rather than introducing randomization.

**Location:**
- None — no `tests/fixtures/`, no `tests/factories.py`. Only pytest's built-in `tmp_path` fixture is used from outside the test file itself.

## Coverage

**Requirements:** None enforced — no coverage tool configured, no CI pipeline detected in the repo.

**View Coverage:**
Not applicable — install `pytest-cov` manually if coverage reporting is needed (`python -m pytest --cov=. --cov-report=term-missing`), it is not part of the existing setup.

## Test Types

**Unit Tests:**
- The entire suite is unit-level: pure-function tests (parsing, formatting, decision logic — no I/O) and DB-integration tests against a real temp SQLite file (still fast, in-process, no network). This is effectively 100% of the current suite.

**Integration Tests:**
- No tests spin up the aiogram `Dispatcher`/polling loop or a real Telegram connection. "Integration" in this codebase means calling a handler function directly with fake `Message`/`CallbackQuery` objects and a real tmp SQLite DB — see `test_admin_phase2.py`'s WR-01/WR-04 tests for the fullest example of this style.

**E2E Tests:**
- Not used. No Telegram Bot API sandbox/mock server, no live-bot smoke tests found.

## Common Patterns

**Async Testing:**
```python
def test_balance_is_sum_of_deltas(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.add_coins(333, 10))
    asyncio.run(db.add_coins(333, -3))
    assert asyncio.run(db.get_balance(333)) == 7
```

**Testing background-task fire-and-forget behavior** (draining `asyncio.all_tasks()` after invoking a handler that spawns a detached task via `services/background.py::spawn`):
```python
async def go():
    await admin.appr_all_yes(cb, None)  # spawns a background task internally
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)

asyncio.run(go())
```

**Error/exception-path testing:**
Fake collaborator objects deliberately raise to verify the handler under test tolerates a failure in a non-critical step (e.g. `_RaisingMessage.edit_text` raising `RuntimeError` to prove welcome-message drain still happens per WR-01, above). This is the standard way this codebase tests fail-soft `try/except` branches — construct a fake that raises where the real dependency could realistically raise, then assert the surrounding logic still completed its critical side effect.

**Filter/predicate testing** (aiogram magic filters, e.g. `F.text.in_(...)`):
```python
filt = F.text.in_({"Отмена"}) | F.text.startswith("/")
def m(t):
    return _types.SimpleNamespace(text=t)
assert filt.resolve(m("/broadcast"))
assert not filt.resolve(m("не подходит по критериям отбора"))
```
Call `.resolve(fake_message)` directly on the constructed `MagicFilter` rather than dispatching a real update — the same "call the piece directly with a minimal fake" philosophy applied to aiogram's filter objects.

---

*Testing analysis: 2026-07-24*
