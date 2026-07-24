# Deferred Items — Phase 05

Out-of-scope discoveries logged during execution (not fixed, per scope-boundary rule).

## 05-01: Missing `apscheduler`/`sqlalchemy` in `.venv` (pre-existing, environment-only)

**Found during:** Task 1 verification (`python -m pytest tests/ -q`)

**Issue:** `tests/test_admin_phase2.py`, `tests/test_broadcast_429_phase3.py`,
`tests/test_coins_phase1.py`, `tests/test_nudge_phase3.py`,
`tests/test_scheduler_helpers_phase3.py` fail to collect with
`ModuleNotFoundError: No module named 'apscheduler'`. `services/scheduler.py` imports
`apscheduler.schedulers.asyncio.AsyncIOScheduler` at module load time.

**Root cause:** `apscheduler==3.11.2` and `sqlalchemy>=2.0,<3.0` are declared in
`requirements.txt` (Phase 3) but not installed into the project's `.venv`.

**Scope determination:** Confirmed pre-existing via `git stash` — the same 5 collection
errors occur on a clean working tree with none of this plan's changes applied. Not caused
by any file this plan (05-01) modified (`database/db.py`, `handlers/registration.py`,
`tests/test_db_phase5.py`). Out of scope per the scope-boundary rule.

**Verification workaround used:** `python -m pytest tests/ -q --ignore=tests/test_admin_phase2.py --ignore=tests/test_broadcast_429_phase3.py --ignore=tests/test_coins_phase1.py --ignore=tests/test_nudge_phase3.py --ignore=tests/test_scheduler_helpers_phase3.py` → 121 passed, 0 regressions from this plan's changes.

**Action needed (not taken here):** `.venv/Scripts/python.exe -m pip install apscheduler==3.11.2 "sqlalchemy>=2.0,<3.0"` to bring the venv in line with `requirements.txt`.
