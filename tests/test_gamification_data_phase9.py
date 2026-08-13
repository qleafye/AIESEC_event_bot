"""Phase 9 Plan 01 (GAME-01/02/03) — interface-first data model.

Covers `game_tasks`/`game_submissions` tables + accessors (Task 1) and the FSM states +
`moderate_game` ADMIN_CAPS map (Task 2). pytest-asyncio is unavailable in this env (see
tests/test_db_phase5.py) — every async helper is driven via asyncio.run(), config.DB_PATH
points at a tmp_path file.

A-05 note (call 13.08, see 09-CONTEXT.md): the deadline is SOFT. `list_active_tasks()` must
NOT filter by deadline_at — a past-deadline task stays visible to a delegate, and
`get_pending_submissions`/`list_all_submissions` expose `task_deadline_at` so the moderation
card can flag "submitted after deadline" without a second query.
"""
import asyncio

from config import config
from database import db


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_gamification_phase9.db")
    asyncio.run(db.init_db())


# ── Task 1: game_tasks / game_submissions tables + accessors ────────────────────────────────

def test_create_and_get_task_roundtrip(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Пост со скрином", "Light", 30, "photo", "2026-08-25 23:59:00", 900801,
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["text"] == "Пост со скрином"
    assert task["category"] == "Light"
    assert task["coins"] == 30
    assert task["proof_type"] == "photo"
    assert task["deadline_at"] == "2026-08-25 23:59:00"


def test_list_active_tasks_keeps_past_deadline(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("Просрочено", "Light", 15, "text", "2020-01-01 00:00:00", None))
    asyncio.run(db.create_task("Скоро", "Medium", 20, "text", "2099-01-01 00:00:00", None))
    tasks = asyncio.run(db.list_active_tasks())
    assert len(tasks) == 2
    assert [t["text"] for t in tasks] == ["Просрочено", "Скоро"]  # ASC deadline_at


def test_create_submission_returns_id_first_time(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "text", "готово", "2026-08-20 10:00:00"))
    assert sub_id is not None


def test_create_submission_blocks_second_pending_for_same_pair(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    first = asyncio.run(db.create_submission(task_id, 111, "text", "a", "2026-08-20 10:00:00"))
    second = asyncio.run(db.create_submission(task_id, 111, "text", "b", "2026-08-20 10:05:00"))
    assert first is not None
    assert second is None
    all_subs = asyncio.run(db.list_all_submissions())
    assert len(all_subs) == 1


def test_create_submission_allows_resubmission_after_rejection(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    first = asyncio.run(db.create_submission(task_id, 111, "text", "a", "2026-08-20 10:00:00"))
    asyncio.run(db.claim_submission(first, 900801, "rejected", reject_reason="дубль"))
    second = asyncio.run(db.create_submission(task_id, 111, "text", "b", "2026-08-20 11:00:00"))
    assert second is not None
    assert second != first
    all_subs = asyncio.run(db.list_all_submissions())
    assert len(all_subs) == 2


def test_get_active_submission_returns_none_after_rejection(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "text", "a", "2026-08-20 10:00:00"))
    active = asyncio.run(db.get_active_submission(task_id, 111))
    assert active is not None and active["id"] == sub_id

    asyncio.run(db.claim_submission(sub_id, 900801, "rejected", reject_reason="дубль"))
    assert asyncio.run(db.get_active_submission(task_id, 111)) is None


def test_get_pending_submissions_joins_task_and_user_fields(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 111, "username": "delegate", "full_name": "Иван Иванов",
        "registration_date": "2026-08-01 00:00:00",
    }))
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2026-08-18 23:59:00", None))
    asyncio.run(db.create_submission(task_id, 111, "text", "готово", "2026-08-20 10:00:00"))
    rows = asyncio.run(db.get_pending_submissions(limit=10, offset=0))
    assert len(rows) == 1
    row = rows[0]
    assert row["task_text"] == "t"
    assert row["task_category"] == "Light"
    assert row["task_coins"] == 15
    assert row["task_proof_type"] == "text"
    assert row["task_deadline_at"] == "2026-08-18 23:59:00"
    assert "user_full_name" in row
    assert "user_username" in row


def test_get_pending_submissions_count_matches_list_length_at_full_limit(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    for uid in range(3):
        asyncio.run(db.create_submission(task_id, 200 + uid, "text", "x", "2026-08-20 10:00:00"))
    count = asyncio.run(db.get_pending_submissions_count())
    rows = asyncio.run(db.get_pending_submissions(limit=count, offset=0))
    assert count == 3
    assert len(rows) == count


def test_claim_submission_approve_race_exactly_one_wins(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "text", "x", "2026-08-20 10:00:00"))

    first = asyncio.run(db.claim_submission(sub_id, 900801, "approved", coins_awarded=30))
    second = asyncio.run(db.claim_submission(sub_id, 900802, "approved", coins_awarded=99))

    assert first is True
    assert second is False
    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 30


def test_claim_submission_reject_stores_reason(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "text", "x", "2026-08-20 10:00:00"))

    ok = asyncio.run(db.claim_submission(sub_id, 900801, "rejected", reject_reason="дубль"))
    assert ok is True
    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["reject_reason"] == "дубль"
    assert submission["reviewed_by"] == 900801
    assert submission["reviewed_at"] is not None


def test_list_all_submissions_ordered_oldest_first_and_joins_task(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_submission(task_id, 111, "text", "a", "2026-08-20 10:00:00"))
    asyncio.run(db.create_submission(task_id, 222, "text", "b", "2026-08-19 10:00:00"))
    rows = asyncio.run(db.list_all_submissions())
    assert [r["submitted_at"] for r in rows] == ["2026-08-19 10:00:00", "2026-08-20 10:00:00"]
    assert all("task_text" in r for r in rows)


def test_get_game_stats_counts_by_status_and_category(tmp_path):
    _db_ready(tmp_path)
    light_task = asyncio.run(db.create_task("light", "Light", 15, "text", "2099-01-01 00:00:00", None))
    medium_task = asyncio.run(db.create_task("medium", "Medium", 20, "text", "2099-01-01 00:00:00", None))

    approved_light = asyncio.run(db.create_submission(light_task, 111, "text", "a", "2026-08-20 10:00:00"))
    asyncio.run(db.claim_submission(approved_light, 900801, "approved", coins_awarded=15))

    approved_medium = asyncio.run(db.create_submission(medium_task, 222, "text", "b", "2026-08-20 10:00:00"))
    asyncio.run(db.claim_submission(approved_medium, 900801, "approved", coins_awarded=20))

    asyncio.run(db.create_submission(light_task, 333, "text", "c", "2026-08-20 10:00:00"))  # pending

    rejected = asyncio.run(db.create_submission(medium_task, 444, "text", "d", "2026-08-20 10:00:00"))
    asyncio.run(db.claim_submission(rejected, 900801, "rejected", reject_reason="дубль"))

    stats = asyncio.run(db.get_game_stats())
    assert stats["participants"] == 4
    assert stats["pending"] == 1
    assert stats["approved"] == 2
    assert stats["rejected"] == 1
    assert stats["by_category"] == {"Light": 1, "Medium": 1}


def test_game_categories_and_proof_types_are_five_and_four_items_respectively():
    assert len(db.GAME_CATEGORIES) == 5
    assert len(db.GAME_PROOF_TYPES) == 4
    assert db.GAME_CATEGORIES == ["Light", "Medium", "Hard", "Referral", "Special"]
    assert db.GAME_PROOF_TYPES == ["photo", "pdf", "text", "link"]


# ── Task 2: FSM states + moderate_game ADMIN_CAPS map ────────────────────────────────────────

def test_game_task_create_states_group_has_expected_steps():
    from handlers.states import GameTaskCreate
    for attr in ("text", "category", "coins", "proof_type", "deadline", "confirm"):
        assert hasattr(GameTaskCreate, attr)


def test_game_review_states_group_has_expected_steps():
    from handlers.states import GameReview
    for attr in ("reject_reason", "approve_amount"):
        assert hasattr(GameReview, attr)


def test_moderate_game_callback_keys_resolve():
    from handlers.admin_caps import required_capability
    callbacks = [
        "admin_game_tasks", "gtnew", "gtcat:foo", "gtproof:photo", "gtconfirm", "gtcancel",
        "admin_game_review", "grev_approve:1", "grev_approve_custom:1", "grev_reject:1",
        "grev_skip:1",
    ]
    for cb in callbacks:
        assert required_capability(callback_data=cb) == "moderate_game", cb


def test_moderate_game_state_keys_resolve():
    from handlers.admin_caps import required_capability
    assert required_capability(raw_state="GameTaskCreate:text") == "moderate_game"
    assert required_capability(raw_state="GameReview:reject_reason") == "moderate_game"
