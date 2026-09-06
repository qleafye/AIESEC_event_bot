"""Квик 260906-7zv (HELP-01/02/03) — редактор подсказок формата под вопросом анкеты
(«💡 Подсказка», экран «✏️ Тексты вопросов»).

Ключ `reg_help_<step>` ГЛОБАЛЬНЫЙ — без осей города и трека (D-1): валидатор ответа,
`reg_engine._validate_answer_core`, один на все города и оба трека (аргумент `participant_type`
в его теле не используется ни разу, `city_code` в него вообще не передаётся) — подсказка,
отличающаяся по городу/треку, могла бы разойтись с ним только во вранье делегату.

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path (та же конвенция, что у
tests/test_admin_percity_prompts_25.py).

Sections (задача 1 — швы движка; экран добавляется задачей 3):
    A — has_help/help_default: базовые случаи + перекрёстная сходимость по всем шагам.
    B — help_text: регрессия (override/пустая строка/дефолт байт-в-байт).
"""
import asyncio

from config import config
from database import db
from handlers import admin_reg_percity
import reg_engine


ADMIN_ID = 920906


def _admin_ready(tmp_path, db_name="test_reg_help_editor_260906.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


# ══════════════════════════════════════════════════════════════════════════════════════════
# A: has_help/help_default — базовые случаи + перекрёстная сходимость
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_has_help_true_for_phone_and_date_step():
    assert reg_engine.has_help("phone") is True
    assert reg_engine.has_help("arrival_date") is True  # тип date


def test_has_help_false_for_goal_and_unknown_step():
    assert reg_engine.has_help("goal") is False
    assert reg_engine.has_help("нет-такого-шага") is False


def test_has_help_matches_help_default_for_every_prompt_step(tmp_path):
    _admin_ready(tmp_path)

    async def scenario():
        for step_key, _label in admin_reg_percity._prompt_steps():
            default = await reg_engine.help_default(step_key, None)
            assert reg_engine.has_help(step_key) == (default is not None), step_key

    asyncio.run(scenario())


def test_help_default_resume_global_and_text_only_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("reg_resume_mode__city__spb", "text_only"))

    assert asyncio.run(reg_engine.help_default("resume", None)) == reg_engine.STEP_HELP["resume"]
    assert asyncio.run(reg_engine.help_default("resume", "spb")) == "Коротко, текстом в чате."


def test_help_default_date_step_returns_shared_date_help(tmp_path):
    _admin_ready(tmp_path)
    assert asyncio.run(reg_engine.help_default("birth_date", None)) == reg_engine._DATE_HELP


# ══════════════════════════════════════════════════════════════════════════════════════════
# B: help_text — регрессия (семантика `or default` байт-в-байт)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_help_text_regression_override_and_empty_reset(tmp_path):
    _admin_ready(tmp_path)

    assert asyncio.run(reg_engine.help_text("vk")) == reg_engine.STEP_HELP["vk"]

    asyncio.run(db.set_setting("reg_help_vk", "Кастом"))
    assert asyncio.run(reg_engine.help_text("vk")) == "Кастом"

    asyncio.run(db.set_setting("reg_help_vk", ""))
    assert asyncio.run(reg_engine.help_text("vk")) == reg_engine.STEP_HELP["vk"]
