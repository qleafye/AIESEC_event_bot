"""Phase 09.1 Plan 02 (GAME-06) — tasks by city.

`game_tasks.event_city` (NULL = all cities), optional `city_scope` kwargs on task/queue
accessors, the "Кому задание?" wizard step (city module gated), the delegate-facing task
list filter, and the moderation-queue city scope + "Город" column on the gamification
sheet tabs.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_scope_phase72.py / tests/test_gamification_data_phase9.py.
"""
import asyncio

from config import config
from database import db
import cities


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_city_tasks_091.db")
    asyncio.run(db.init_db())


# ── Task 1: game_tasks.event_city + city_scope kwarg on tasks/queue ─────────────────────────

def test_city_clause_no_new_args_matches_old_pair_byte_for_byte():
    """Regression: `_city_clause` without the new args returns the exact same pair the
    pre-09.1 applications/receipts queries depend on."""
    assert db._city_clause(("msk", ("spb", "tyumen"))) == (
        "(event_city IS NULL OR event_city NOT IN (?, ?))", ["spb", "tyumen"],
    )
    assert db._city_clause(("spb", ())) == ("event_city = ?", ["spb"])
    assert db._city_clause(None) == ("", [])


def test_city_clause_column_kwarg_qualifies_both_branches():
    frag, params = db._city_clause(("msk", ("spb",)), "t.event_city")
    assert frag == "(t.event_city IS NULL OR t.event_city NOT IN (?))"
    assert params == ["spb"]
    frag2, params2 = db._city_clause(("spb", ()), "t.event_city")
    assert frag2 == "t.event_city = ?"
    assert params2 == ["spb"]


def test_city_clause_include_null_equality_branch():
    frag, params = db._city_clause(("spb", ()), "t.event_city", include_null=True)
    assert frag == "(t.event_city IS NULL OR t.event_city = ?)"
    assert params == ["spb"]


def test_city_clause_include_null_is_noop_for_exclusion_branch():
    frag, params = db._city_clause(("msk", ("spb",)), "event_city", include_null=True)
    assert frag == "(event_city IS NULL OR event_city NOT IN (?))"
    assert params == ["spb"]


def test_create_task_with_event_city_saves_city(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "spb task", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb",
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["event_city"] == "spb"


def test_create_task_without_kwarg_saves_null(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "old style", "Light", 10, "text", "2099-01-01 00:00:00", None,
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["event_city"] is None


def test_list_active_tasks_city_scope_includes_null_excludes_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    tasks = asyncio.run(db.list_active_tasks(city_scope=cities.city_scope("spb"), include_null=True))
    texts = {t["text"] for t in tasks}
    assert texts == {"all", "spb"}


def test_list_active_tasks_default_city_scope_includes_null(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    tasks = asyncio.run(db.list_active_tasks(city_scope=cities.city_scope("msk"), include_null=True))
    texts = {t["text"] for t in tasks}
    assert texts == {"all", "msk"}


def test_list_active_tasks_no_kwarg_returns_everything(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    tasks = asyncio.run(db.list_active_tasks())
    assert len(tasks) == 3


def _seed_submission(task_id, user_id, user_city):
    asyncio.run(db.add_user({
        "telegram_id": user_id,
        "full_name": f"User {user_id}",
        "registration_date": "2026-01-01 09:00:00",
        "event_city": user_city,
    }))
    return asyncio.run(db.create_submission(
        task_id, user_id, "text", "готово", "2026-08-20 10:00:00",
    ))


def test_get_pending_submissions_city_scope_filters_by_delegate_city(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    _seed_submission(task_id, 102, "msk")
    _seed_submission(task_id, 103, None)
    rows = asyncio.run(db.get_pending_submissions(limit=50, offset=0, city_scope=cities.city_scope("spb")))
    assert {r["user_id"] for r in rows} == {101}


def test_get_pending_submissions_count_consistent_with_list(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    _seed_submission(task_id, 102, "msk")
    _seed_submission(task_id, 103, None)
    scope = cities.city_scope("msk")
    rows = asyncio.run(db.get_pending_submissions(limit=50, offset=0, city_scope=scope))
    count = asyncio.run(db.get_pending_submissions_count(city_scope=scope))
    assert count == len(rows) == 2  # msk delegate + no-city delegate (exclusion form)


def test_list_all_submissions_exposes_user_event_city(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    rows = asyncio.run(db.list_all_submissions())
    assert rows[0]["user_event_city"] == "spb"


def test_get_pending_submissions_no_scope_matches_today(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    _seed_submission(task_id, 102, "msk")
    rows = asyncio.run(db.get_pending_submissions(limit=50, offset=0))
    assert len(rows) == 2
