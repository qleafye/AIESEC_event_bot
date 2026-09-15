"""Квик 260915-skg, Задача 1 (P1/P1b/P3): возраст числом в Mini App, общий guard не-строковых
ответов в `_validate_answer_core` (тумблер «Образование» шлёт bool -> AttributeError -> 500,
`<input type="number">` шлёт JSON-число), серверная спека карточки «Образование» отдаёт
`studying_option`/`not_studying_options`, человеческие дефолты трёх текстов реестра.

pytest-asyncio в окружении нет (см. шапку `tests/test_menu_i18n_260912.py`) — только
`asyncio.run()`.
"""
import asyncio

from config import config
from database.db import init_db, set_setting

from reg_engine import (
    _composite_spec_for,
    parse_age,
    step_spec,
    validate_answer,
)
from settings_schema import SETTINGS_SCHEMA

AGE_ERROR = "Укажи корректный возраст числом от 10 до 120."
EDU_ERROR = "Выбери один из вариантов."

_ALL_ON_FLAGS = {
    "v2_enabled": True, "chips": True, "lookup_search": True, "edu_card": True,
    "repeatable": True, "limit_counter": True, "status_screen": True,
    "header_settings": True, "haptics": True,
}


def _ready(tmp_path, name="reg_composite_edu_260915.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(init_db())


# ── parse_age: int/str/bool/None, границы 10..120 не двигаются ─────────────────────────────

def test_parse_age_accepts_int_and_str():
    assert parse_age(17) == 17
    assert parse_age("17") == 17


def test_parse_age_rejects_bool_explicitly():
    assert parse_age(True) is None
    assert parse_age(False) is None


def test_parse_age_rejects_none():
    assert parse_age(None) is None


def test_parse_age_boundaries_unchanged():
    assert parse_age(9) is None
    assert parse_age(10) == 10
    assert parse_age(120) == 120
    assert parse_age(121) is None


# ── validate_answer("age", ...): JSON-число даёт значение, bool -- штатную ошибку ──────────

def test_validate_answer_age_accepts_int():
    assert validate_answer("age", 17) == (17, None)


def test_validate_answer_age_bool_gives_soft_error_not_exception():
    value, error = validate_answer("age", True)
    assert value is None
    assert error == AGE_ERROR


# ── validate_answer("education_status", ...): bool/dict/list -- штатная ошибка, не 500 ─────

def test_validate_answer_education_status_bool_true_gives_soft_error():
    value, error = validate_answer("education_status", True)
    assert value is None
    assert error == EDU_ERROR


def test_validate_answer_education_status_bool_false_gives_soft_error():
    value, error = validate_answer("education_status", False)
    assert value is None
    assert error == EDU_ERROR


def test_validate_answer_education_status_dict_gives_soft_error():
    value, error = validate_answer("education_status", {})
    assert value is None
    assert error == EDU_ERROR


def test_validate_answer_education_status_list_gives_soft_error():
    value, error = validate_answer("education_status", [])
    assert value is None
    assert error == EDU_ERROR


# ── Guard не трогает multi/repeatable — байт-в-байт прежнее поведение ──────────────────────

def test_validate_answer_multi_step_list_untouched_by_guard():
    value, error = validate_answer("formats", ["Кейс-чемпионат", "Мастер-класс"])
    assert error is None
    assert value == "Кейс-чемпионат, Мастер-класс"


def test_validate_answer_multi_step_empty_list_still_errors_as_before():
    value, error = validate_answer("formats", [])
    assert value is None
    assert error == "Выбери хотя бы один вариант."


def test_validate_answer_mini_portfolio_list_of_blocks_untouched_by_guard():
    value, error = validate_answer(
        "mini_portfolio", [{"title": "Кейс", "description": "Описание"}],
    )
    assert error is None
    assert "Кейс" in value


# ── _composite_spec_for: studying_option / not_studying_options ────────────────────────────

def test_composite_spec_exposes_studying_option_and_not_studying_options(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(step_spec("education_status", None, None, flags=_ALL_ON_FLAGS))
    comp = spec["composite"]
    assert comp["studying_option"], "studying_option не должен быть пустым при дефолтных опциях"
    assert comp["studying_option"].startswith("Да")
    assert isinstance(comp["not_studying_options"], list)
    assert comp["studying_option"] not in comp["not_studying_options"]
    assert len(comp["not_studying_options"]) >= 1
    for opt in comp["not_studying_options"]:
        assert not opt.startswith("Да")


def test_composite_spec_studying_option_none_when_options_list_empty(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def _empty_options(step_key):
        return []

    import reg_engine
    monkeypatch.setattr(reg_engine, "options", _empty_options)
    spec = asyncio.run(_composite_spec_for("education", None, None, _ALL_ON_FLAGS))
    assert spec["studying_option"] is None
    assert spec["not_studying_options"] == []


def test_composite_spec_respects_registry_studying_statuses(tmp_path):
    """D-06: правило «кто считается учащимся» живёт только в `is_studying`/`edu_studying_statuses`
    — не хардкод в _composite_spec_for."""
    _ready(tmp_path)
    asyncio.run(set_setting(
        "education_status_options",
        "Учусь очно\nУчусь заочно\nЗавершил обучение\nНе получал образование",
    ))
    asyncio.run(set_setting("edu_studying_statuses", "Учусь очно\nУчусь заочно"))
    spec = asyncio.run(step_spec("education_status", None, None, flags=_ALL_ON_FLAGS))
    comp = spec["composite"]
    assert comp["studying_option"] == "Учусь очно"
    assert "Учусь заочно" in comp["not_studying_options"]
    assert "Завершил обучение" in comp["not_studying_options"]
    assert "Не получал образование" in comp["not_studying_options"]


# ── Тексты реестра: человеческие, без слов дизайн-спеки ────────────────────────────────────

def test_composite_texts_defaults_are_human_not_design_spec_annotations():
    subtitle = SETTINGS_SCHEMA["reg_composite_edu_subtitle_text"]["default"]
    done_hint = SETTINGS_SCHEMA["reg_composite_edu_done_hint_text"]["default"]
    toggle_note = SETTINGS_SCHEMA["reg_composite_toggle_note_text"]["default"]

    for text in (subtitle, done_hint, toggle_note):
        assert "скипать" not in text.lower()
        assert "вопросов подряд" not in text.lower()
        assert "шагов просто нет" not in text.lower()

    assert toggle_note == ""
    assert subtitle.strip()
    assert done_hint.strip()
