"""Quick 260904-de4 (D1) — подсказка формата на шагах веб-анкеты, где формат есть.

`step_spec()["help"]` — новый ключ контракта UI-SPEC (см. `reg_engine.help_text`). Подсказка
обязана называть формат, который реально принимает `_validate_answer_core` для этого шага —
проверяется прогоном `validate_answer(step, STEP_HELP_EXAMPLES[step])` по каждому шагу с
подсказкой: пример не проходит свой же валидатор — подсказка врёт.

БД поднимается как в `tests/test_reg_engine_labels.py::_ready` (pytest-asyncio недоступен —
async через `asyncio.run()`, правило проекта).
"""
from __future__ import annotations

import asyncio

import pytest

import database.db as db
import reg_engine
from config import config
from database.db import init_db


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reg_step_help_260904.db")
    asyncio.run(init_db())


@pytest.mark.parametrize("step_key", ["vk", "phone", "email", "full_name", "age", "resume"])
def test_steps_with_format_have_nonempty_help(tmp_path, step_key):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec(step_key))
    assert spec["help"]
    assert isinstance(spec["help"], str)


def test_vk_help_names_at_username_format(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec("vk"))
    assert "@username" in spec["help"] or "@" in spec["help"]


@pytest.mark.parametrize("step_key", ["arrival_date", "birth_date", "payment_plan_date"])
def test_date_steps_get_shared_date_help(tmp_path, step_key):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec(step_key))
    assert spec["help"] == reg_engine._DATE_HELP
    assert "ДД.ММ.ГГГГ" in spec["help"]


@pytest.mark.parametrize("step_key", ["city", "education_status"])
def test_steps_without_format_have_no_help(tmp_path, step_key):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec(step_key))
    assert spec["help"] is None


def test_choice_step_with_options_has_no_help(tmp_path):
    _ready(tmp_path)
    # Любой choice со списком вариантов — без своей записи в STEP_HELP и не типа date.
    step_key = "attendance_format"
    assert reg_engine.REG_STEP_TYPES.get(step_key) != "date"
    spec = asyncio.run(reg_engine.step_spec(step_key))
    assert spec["help"] is None


def test_examples_pass_their_own_validator():
    for step_key, example in reg_engine.STEP_HELP_EXAMPLES.items():
        value, error = reg_engine.validate_answer(step_key, example)
        assert error is None, f"{step_key}: {error}"


def test_help_override_beats_default(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("reg_help_vk", "Кастомная подсказка про ВК"))
    spec = asyncio.run(reg_engine.step_spec("vk"))
    assert spec["help"] == "Кастомная подсказка про ВК"


def test_empty_override_falls_back_to_default(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("reg_help_vk", ""))
    spec = asyncio.run(reg_engine.step_spec("vk"))
    assert spec["help"] == reg_engine.STEP_HELP["vk"]


@pytest.mark.parametrize("step_key", ["age", "phone", "resume"])
def test_prompt_text_unchanged_by_help_addition(tmp_path, step_key):
    _ready(tmp_path)
    assert asyncio.run(reg_engine.prompt(step_key)) == reg_engine.PROMPT_DEFAULTS[step_key]
