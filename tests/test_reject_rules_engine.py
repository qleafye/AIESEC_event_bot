"""Phase 31 План 01 (D-01/D-02/D-03/D-04/D-06/D-07/D-14/D-31/D-32): чистый оценщик правил
автоотказа — ядро всей фазы 31.

Три группы сторожей:
- `age_on` — возраст на ПРОИЗВОЛЬНУЮ дату (D-31), не «сегодня»: обычный случай, день рождения
  ещё не наступил/наступил ровно в день форума, високосный 29.02, битые/пустые входы.
- `reject_condition_category`/`condition_operators`/`rule_pause_reason` — какой набор
  операторов предложить менеджеру по типу шага (D-01) и когда правило само встаёт на паузу
  (D-14).
- `evaluate_reject_rules` — группы И/ИЛИ (D-02), каждый оператор по одному разу, отказ сильнее
  пометки (D-04), «Магистратура/Аспирантура» не попадает под правило по курсу (D-32), делегат
  прошлого сезона оценивается так же (D-06), оценщик структурно не может вернуть «одобрено»
  (D-03), падение внутри условия не роняет весь оценщик (T-31-01-01).

Все проверяемые функции чистые (без БД, без aiogram, без единого `await` — D-07) — файл
обходится без временной БД и без `asyncio.run`, в отличие от `tests/test_skillup_scoring_28.py`
(её образец структуры — докстринг, табличные фикстуры, обычные `def test_...`), которому DB
нужна для смежных, не-чистых кусков её собственной фазы.
"""
import inspect

import pytest

import reg_engine


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: age_on — возраст на произвольную дату
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_age_on_ordinary_case():
    assert reg_engine.age_on("01.01.2008", "15.10.2026") == 18


def test_age_on_birthday_not_yet_occurred():
    # Родился 20.12 — на 15.10 в тот же год день рождения ещё не наступил.
    assert reg_engine.age_on("20.12.2008", "15.10.2026") == 17


def test_age_on_birthday_exactly_on_target_date():
    assert reg_engine.age_on("15.10.2008", "15.10.2026") == 18


def test_age_on_leap_birthday_not_yet_occurred():
    # 29.02 — день рождения ещё не наступил (28.02 раньше 29.02).
    assert reg_engine.age_on("29.02.2008", "28.02.2026") == 17


def test_age_on_leap_birthday_already_occurred():
    # 01.03 — 29.02 уже прошло (в невисокосном году де-факто «прошло 1 марта»).
    assert reg_engine.age_on("29.02.2008", "01.03.2026") == 18


def test_age_on_empty_or_garbage_birth_date():
    assert reg_engine.age_on("", "15.10.2026") is None
    assert reg_engine.age_on("не дата", "15.10.2026") is None
    assert reg_engine.age_on(None, "15.10.2026") is None


def test_age_on_empty_target_date():
    assert reg_engine.age_on("01.01.2008", "") is None
    assert reg_engine.age_on("01.01.2008", None) is None


def test_age_on_target_before_birth():
    assert reg_engine.age_on("01.01.2020", "15.10.2010") is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: reject_condition_category / condition_operators / rule_pause_reason
# ══════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("step_key,expected_category", [
    ("course", "select"),
    ("study_field", "select"),
    ("stack", "multi"),
    ("age", "int"),
    ("birth_date", "birth_date"),
    ("arrival_date", "date"),
    ("resume", "file"),
    ("expectations", "text"),
])
def test_reject_condition_category(step_key, expected_category):
    assert reg_engine.reject_condition_category(step_key) == expected_category


def test_operator_sets_are_closed():
    assert reg_engine.REJECT_RULE_OPERATORS["select"] == ("in", "not_in")
    assert reg_engine.REJECT_RULE_OPERATORS["multi"] == ("in", "not_in")
    assert reg_engine.REJECT_RULE_OPERATORS["int"] == ("lt", "gt", "between")
    assert reg_engine.REJECT_RULE_OPERATORS["date"] == ("before", "after")
    assert reg_engine.REJECT_RULE_OPERATORS["birth_date"] == ("before", "after", "age_on_forum_lt")
    assert reg_engine.REJECT_RULE_OPERATORS["text"] == ("filled", "empty")
    assert reg_engine.REJECT_RULE_OPERATORS["file"] == ("has_file", "no_file")


def test_condition_operators_is_thin_wrapper():
    assert reg_engine.condition_operators("course") == ("in", "not_in")
    assert reg_engine.condition_operators("birth_date") == ("before", "after", "age_on_forum_lt")
    assert reg_engine.condition_operators("resume") == ("has_file", "no_file")


_COURSE_RULE = {
    "id": 1, "name": None, "city": None, "tracks": ["full"],
    "conditions": [[{"step": "course", "op": "in", "values": ["1", "2"]}]],
    "action": "reject", "reject_text": "Места на 1-2 курс закончились.",
    "enabled": 1, "paused_reason": None,
}


def test_rule_pause_reason_on_disabled_question():
    reason = reg_engine.rule_pause_reason(_COURSE_RULE, enabled_steps=[], options_by_step={})
    assert reason is not None
    assert "Курс" in reason  # человеческая подпись, не сырой step_key


def test_rule_pause_reason_on_vanished_option():
    reason = reg_engine.rule_pause_reason(
        _COURSE_RULE,
        enabled_steps=["course"],
        options_by_step={"course": ["3", "4", "5+"]},  # «1»/«2» больше нет среди вариантов
    )
    assert reason is not None
    assert "Курс" in reason


def test_rule_pause_reason_healthy_rule():
    reason = reg_engine.rule_pause_reason(
        _COURSE_RULE,
        enabled_steps=["course"],
        options_by_step={"course": ["1", "2", "3", "4", "5+", "Магистратура/Аспирантура"]},
    )
    assert reason is None


def test_rule_pause_reason_step_missing_from_options_by_step():
    # Список вариантов этого шага просто не собрали — значения условия НЕ проверяются.
    reason = reg_engine.rule_pause_reason(
        _COURSE_RULE, enabled_steps=["course"], options_by_step={},
    )
    assert reason is None


def test_rule_pause_reason_empty_rule_is_healthy():
    empty_rule = {**_COURSE_RULE, "conditions": []}
    assert reg_engine.rule_pause_reason(empty_rule, enabled_steps=[], options_by_step={}) is None
