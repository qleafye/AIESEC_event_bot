"""Фикс фазы 32 (CR-02): `services/game_award.py::award_for` — вынесенная копия
`handlers/admin_gamification.py::_award_for`, aiogram-free, чтобы её мог позвать и бот, и
Mini App. Числа здесь обязаны совпадать с `tests/test_game_late_penalty_32.py` (тот же
процент, тот же результат) — иначе формула фактически разъехалась бы при переносе.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from services.game_award import award_for

ADMIN_ID = 932960


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_award_32fix.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _set(key, value):
    asyncio.run(db.set_setting(key, value))


def test_award_for_penalizes_late_submission_same_as_bot_formula(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task = {"id": 1, "coins": 100, "deadline_at": "2026-08-10 23:59:00"}
    submission = {"submitted_at": "2026-08-14 10:00:00"}
    coins, late = asyncio.run(award_for(submission, task, task["coins"]))
    assert (coins, late) == (70, True)


def test_award_for_on_time_submission_not_penalized(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task = {"id": 1, "coins": 100, "deadline_at": "2026-08-25 23:59:00"}
    submission = {"submitted_at": "2026-08-14 10:00:00"}
    coins, late = asyncio.run(award_for(submission, task, task["coins"]))
    assert (coins, late) == (100, False)


def test_award_for_zero_percent_matches_pre_phase_amount(tmp_path):
    """Процент штрафа не задан (дефолт 0) -- число не меняется ни на балл, `late` всё равно
    True (сдача правда была после дедлайна, просто без вычета) -- та же пара, что вернул бы
    `_award_for` бота на этих же входных данных."""
    _db_ready(tmp_path)
    task = {"id": 1, "coins": 100, "deadline_at": "2026-08-10 23:59:00"}
    submission = {"submitted_at": "2026-08-14 10:00:00"}
    coins, late = asyncio.run(award_for(submission, task, task["coins"]))
    assert (coins, late) == (100, True)


def test_award_for_no_deadline_task_never_late():
    task = {"id": 1, "coins": 100, "deadline_at": "9999-12-31 23:59:59"}  # NO_DEADLINE_AT
    submission = {"submitted_at": "2026-08-14 10:00:00"}
    coins, late = asyncio.run(award_for(submission, task, task["coins"]))
    assert (coins, late) == (100, False)


def test_award_for_custom_amount_is_penalized_same_percent(tmp_path):
    """Своя сумма — тот же путь, что дефолтная (паритет с
    `grev_approve_amount_step`/`test_grev_approve_amount_step_custom_200_late_becomes_140`)."""
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task = {"id": 1, "coins": 100, "deadline_at": "2026-08-10 23:59:00"}
    submission = {"submitted_at": "2026-08-14 10:00:00"}
    coins, late = asyncio.run(award_for(submission, task, 200))
    assert (coins, late) == (140, True)
