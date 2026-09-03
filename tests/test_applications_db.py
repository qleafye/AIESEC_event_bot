"""Phase 23 Plan 01 Task 1 (APP-TINDER-01, D-06/D-08): снимок поведения фильтров очереди
заявок и журнала решений раньше самой реализации.

До задачи 2 этот файл красный: `get_pending_users`/`get_pending_count` ещё не принимают
`track`/`changed_only`, `record_application_decision`/`claim_application_undo`/
`claim_due_application_decisions`/`revert_user_to_pending`/`set_user_avatar`/
`find_user_by_avatar_file_id` в `database/db.py` ещё не существуют — `TypeError`/`AttributeError`.

pytest-asyncio в проекте не используется — асинхронщина через `asyncio.run()`, БД — временная
(`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), как в `tests/test_miniapp_review.py`
и `tests/test_admin_phase5.py`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "applications_db.db")


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, participant_type=None, status="pending", edited_at=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
        "participant_type": participant_type,
    }))
    _run(db.set_user_status(tid, status))
    if edited_at:
        _run(db.mark_user_edited(tid, "bot"))


# ── Обратная совместимость: старые вызовы бота без новых аргументов ─────────────────────────

def test_get_pending_users_without_new_args_unchanged(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1001, participant_type="full")
    _seed_user(1002, participant_type="short")

    rows = _run(db.get_pending_users(limit=10, offset=0))
    assert [r["telegram_id"] for r in rows] == [1001, 1002]


def test_get_pending_count_without_new_args_unchanged(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2001, participant_type="full")
    _seed_user(2002, participant_type="party_overnight")
    _seed_user(2003, status="approved")  # не в очереди

    count = _run(db.get_pending_count())
    assert count == 2


# ── Фильтр трека ──────────────────────────────────────────────────────────────────────────

def test_track_full_includes_null_and_full(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(3001, participant_type="full")
    _seed_user(3002, participant_type=None)
    _seed_user(3003, participant_type="short")

    rows = _run(db.get_pending_users(limit=10, offset=0, track="full"))
    assert {r["telegram_id"] for r in rows} == {3001, 3002}


def test_track_party_includes_both_party_variants(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(3101, participant_type="party_overnight")
    _seed_user(3102, participant_type="party_noovernight")
    _seed_user(3103, participant_type="full")

    rows = _run(db.get_pending_users(limit=10, offset=0, track="party"))
    assert {r["telegram_id"] for r in rows} == {3101, 3102}


def test_track_short_only(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(3201, participant_type="short")
    _seed_user(3202, participant_type="full")

    rows = _run(db.get_pending_users(limit=10, offset=0, track="short"))
    assert {r["telegram_id"] for r in rows} == {3201}


def test_track_unknown_value_means_no_filter(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(3301, participant_type="full")
    _seed_user(3302, participant_type="short")

    rows = _run(db.get_pending_users(limit=10, offset=0, track="not-a-real-track"))
    assert {r["telegram_id"] for r in rows} == {3301, 3302}


# ── Фильтр «только изменённые» ───────────────────────────────────────────────────────────

def test_changed_only_keeps_edited_rows(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(3401, participant_type="full", edited_at=True)
    _seed_user(3402, participant_type="full")

    rows = _run(db.get_pending_users(limit=10, offset=0, changed_only=True))
    assert [r["telegram_id"] for r in rows] == [3401]

    count = _run(db.get_pending_count(changed_only=True))
    assert count == len(rows)


def test_track_and_changed_only_combine_in_one_where(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(3501, participant_type="full", edited_at=True)
    _seed_user(3502, participant_type="full")
    _seed_user(3503, participant_type="short", edited_at=True)

    rows = _run(db.get_pending_users(limit=10, offset=0, track="full", changed_only=True))
    assert [r["telegram_id"] for r in rows] == [3501]


# ── Журнал решений: заявление, отмена, сметание просроченных ────────────────────────────────

def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_record_application_decision_returns_positive_id(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    now = datetime.now()
    did = _run(db.record_application_decision(
        4001, "approved", None, 999, _iso(now), _iso(now + timedelta(seconds=5)),
    ))
    assert did > 0


def test_claim_due_application_decisions_only_when_due(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    now = datetime.now()
    did = _run(db.record_application_decision(
        4101, "approved", None, 999, _iso(now), _iso(now + timedelta(seconds=5)),
    ))

    not_yet = _run(db.claim_due_application_decisions(_iso(now)))
    assert not_yet == []

    later = now + timedelta(seconds=10)
    due = _run(db.claim_due_application_decisions(_iso(later)))
    assert [row["id"] for row in due] == [did]


def test_claim_application_undo_exactly_once(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    now = datetime.now()
    did = _run(db.record_application_decision(
        4201, "approved", None, 999, _iso(now), _iso(now + timedelta(seconds=5)),
    ))

    first = _run(db.claim_application_undo(did))
    assert first is not None
    assert first["id"] == did

    second = _run(db.claim_application_undo(did))
    assert second is None


def test_claim_application_undo_none_after_effects_sent(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    now = datetime.now()
    did = _run(db.record_application_decision(
        4301, "approved", None, 999, _iso(now), _iso(now)),
    )

    due = _run(db.claim_due_application_decisions(_iso(now + timedelta(seconds=1))))
    assert [row["id"] for row in due] == [did]

    undo = _run(db.claim_application_undo(did))
    assert undo is None


def test_claim_due_application_decisions_never_returns_undone(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    now = datetime.now()
    did = _run(db.record_application_decision(
        4401, "approved", None, 999, _iso(now), _iso(now)),
    )

    undo = _run(db.claim_application_undo(did))
    assert undo is not None

    due = _run(db.claim_due_application_decisions(_iso(now + timedelta(seconds=1))))
    assert due == []


def test_claim_due_application_decisions_returns_each_row_once(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    now = datetime.now()
    did = _run(db.record_application_decision(
        4501, "approved", None, 999, _iso(now), _iso(now)),
    )

    first = _run(db.claim_due_application_decisions(_iso(now + timedelta(seconds=1))))
    assert [row["id"] for row in first] == [did]

    second = _run(db.claim_due_application_decisions(_iso(now + timedelta(seconds=1))))
    assert second == []


# ── Откат в pending ───────────────────────────────────────────────────────────────────────

def test_revert_user_to_pending_success_then_false(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4601, participant_type="full", status="approved")

    ok = _run(db.revert_user_to_pending(4601, "approved"))
    assert ok is True

    row = _run(db.get_pending_users(limit=10, offset=0))
    assert 4601 in [r["telegram_id"] for r in row]

    again = _run(db.revert_user_to_pending(4601, "approved"))
    assert again is False


def test_revert_user_to_pending_wrong_from_status_no_op(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4701, participant_type="full", status="approved")

    ok = _run(db.revert_user_to_pending(4701, "rejected"))
    assert ok is False

    rows = _run(db.get_pending_users(limit=10, offset=0))
    assert 4701 not in [r["telegram_id"] for r in rows]


# ── Аватар ───────────────────────────────────────────────────────────────────────────────

def test_set_and_find_user_avatar(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4801, participant_type="full")

    _run(db.set_user_avatar(4801, "AgAD_fake_file_id", "2026-09-03 00:00:00"))

    found = _run(db.find_user_by_avatar_file_id("AgAD_fake_file_id"))
    assert found is not None
    assert found["telegram_id"] == 4801


def test_find_user_by_avatar_file_id_unknown_returns_none(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())

    found = _run(db.find_user_by_avatar_file_id("no-such-file-id"))
    assert found is None
