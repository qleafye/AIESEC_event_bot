"""Приёмка 15.09 (Анкета 2.0 на стенде, девять тумблеров `reg_form_*` включены): карточка
«Образование» поглощает свои под-вопросы, «шаг N из M» считает то, что делегат реально увидит,
ответы «да/нет» показываются словами, а плитка «Другое» знает, что обязана раскрыть поле.

pytest-asyncio в окружении нет (см. шапку `tests/test_reg_composite_edu_260915.py`) — только
`asyncio.run()`.
"""
import asyncio

from config import config
from database.db import init_db, set_setting

import reg_engine
from reg_engine import (
    advance_anchor,
    composite_absorbed_steps,
    form_spec,
    step_spec,
)

_EDU_PARTS = ("university", "course", "study_field")

_ALL_ON_FLAGS = {
    "v2_enabled": True, "chips": True, "lookup_search": True, "edu_card": True,
    "repeatable": True, "limit_counter": True, "status_screen": True,
    "header_settings": True, "haptics": True,
}
_ALL_OFF_FLAGS = {name: False for name in reg_engine.FORM_V2_TOGGLE_KEYS}


def _ready(tmp_path, name="reg_form_v2_uat_260915.db", *, v2=True):
    """Стенд приёмки: девять тумблеров `reg_form_*` включены, `edu_conditional` выключен
    (иначе ВУЗ/курс/программа не попадают в набор шагов, пока делегат не ответил «учусь», и
    поглощать нечего), вопрос «Амбассадор» включён (дефолт `off`)."""
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(init_db())
    for key in reg_engine.FORM_V2_TOGGLE_KEYS:
        asyncio.run(set_setting(f"reg_form_{key}", "on" if v2 else "off"))
    asyncio.run(set_setting("edu_conditional", "off"))
    asyncio.run(set_setting("reg_q_ambassador", "on"))


def _steps(answers=None):
    spec = asyncio.run(form_spec(answers or {}, "full", None))
    return spec, [row["key"] for row in spec["steps"]]


# ── п.3б «дальше почему-то пошёл вопрос про вуз» ───────────────────────────────────────────

def test_composite_parts_are_not_separate_steps_when_card_is_on(tmp_path):
    _ready(tmp_path)
    _spec, keys = _steps()
    assert "education_status" in keys, "шаг-карточка обязан остаться"
    for part in _EDU_PARTS:
        assert part not in keys, f"{part} рисуется ВНУТРИ карточки, второго экрана быть не должно"


def test_composite_parts_stay_separate_steps_when_card_is_off(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_off.db", v2=False)
    _spec, keys = _steps()
    assert "education_status" in keys
    for part in _EDU_PARTS:
        assert part in keys, "выключенная карточка = четыре обычных шага, как сегодня"


def test_absorbed_steps_empty_without_card_flag():
    assert composite_absorbed_steps(_ALL_OFF_FLAGS) == {}
    assert composite_absorbed_steps({**_ALL_ON_FLAGS, "edu_card": False}) == {}


def test_absorbed_steps_map_parts_to_toggle_step():
    absorbed = composite_absorbed_steps(_ALL_ON_FLAGS)
    assert absorbed == {part: "education_status" for part in _EDU_PARTS}


# ── п.6 «сбита нумерация» ──────────────────────────────────────────────────────────────────

def test_progress_total_counts_only_steps_delegate_will_see(tmp_path):
    _ready(tmp_path)
    spec, keys = _steps()
    assert spec["progress"]["total"] == len(keys)
    assert len(keys) == len(set(keys)), "дублей шагов в мастере быть не должно"


def test_card_shrinks_step_count_by_number_of_absorbed_parts(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_count_on.db")
    _spec_on, keys_on = _steps()
    _ready(tmp_path, "reg_form_v2_uat_count_off.db", v2=False)
    _spec_off, keys_off = _steps()
    absorbed_present = [part for part in _EDU_PARTS if part in keys_off]
    assert absorbed_present, "фикстура обязана включать хотя бы одну часть карточки"
    assert len(keys_off) - len(keys_on) == len(absorbed_present)


# ── п.3б/п.3в: следующий шаг после карточки — тот, что ЗА группой ─────────────────────────

def test_advance_anchor_jumps_over_the_whole_education_group(tmp_path):
    _ready(tmp_path)
    enabled = asyncio.run(reg_engine.enabled_steps({"participant_type": "full"}))
    anchor = advance_anchor("education_status", enabled, _ALL_ON_FLAGS)
    group = [key for key in enabled if key in ("education_status",) + _EDU_PARTS]
    assert anchor == group[-1]
    assert anchor != "education_status", "иначе мастер переспрашивает ВУЗ отдельным экраном"


def test_advance_anchor_is_identity_when_card_is_off(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_anchor_off.db", v2=False)
    enabled = asyncio.run(reg_engine.enabled_steps({"participant_type": "full"}))
    assert advance_anchor("education_status", enabled, _ALL_OFF_FLAGS) == "education_status"


def test_advance_anchor_is_identity_for_step_outside_any_group():
    assert advance_anchor("age", ["age", "phone"], _ALL_ON_FLAGS) == "age"


# ── п.8 «на итоговом просмотре анкеты амбассадор false» ───────────────────────────────────

def test_boolean_answer_gets_human_display(tmp_path):
    _ready(tmp_path)
    # `answers` — словарь КОЛОНОК (так его собирает черновик/`answers_from_user_row`), у шага
    # «Амбассадор» колонка называется иначе, чем шаг.
    column = reg_engine.STEP_TO_COLUMN["ambassador"]
    spec_yes, _keys = _steps({column: True})
    row_yes = next(row for row in spec_yes["steps"] if row["key"] == "ambassador")
    assert row_yes["value"] is True
    assert row_yes["display"] == "Да"

    spec_no, _keys = _steps({column: False})
    row_no = next(row for row in spec_no["steps"] if row["key"] == "ambassador")
    assert row_no["display"] == "Нет"


def test_text_answer_has_no_display_override(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_display_text.db")
    spec, _keys = _steps({"age": 19})
    row = next(row for row in spec["steps"] if row["key"] == "age")
    assert row.get("display") in (None, "")


# ── п.5 «Другое: напиши свой вариант — ПИСАТЬ НЕГДЕ» ──────────────────────────────────────

def test_other_option_published_for_step_with_other_in_options(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(step_spec("source", "full", None, flags=_ALL_ON_FLAGS))
    assert spec["degraded_kind"] == "select"
    assert spec["other_option"] == reg_engine.OTHER_OPTION
    assert reg_engine.OTHER_OPTION in spec["options"]


def test_other_option_published_for_other_allowed_step(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_other_allowed.db")
    spec = asyncio.run(step_spec("local_committee", "full", None, flags=_ALL_ON_FLAGS))
    assert spec["other_allowed"] is True
    assert spec["other_option"] == reg_engine.OTHER_OPTION


def test_other_option_absent_for_closed_list_step(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_other_closed.db")
    spec = asyncio.run(step_spec("alumni_status", "full", None, flags=_ALL_ON_FLAGS))
    assert "other_option" not in spec


def test_other_option_absent_when_new_form_is_off(tmp_path):
    _ready(tmp_path, "reg_form_v2_uat_other_off.db", v2=False)
    spec = asyncio.run(step_spec("source", "full", None, flags=_ALL_OFF_FLAGS))
    assert spec["degraded_kind"] == "legacy"
    assert "other_option" not in spec
