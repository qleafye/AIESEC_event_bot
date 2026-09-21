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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: evaluate_reject_rules — группы И/ИЛИ по всем операторам
# ══════════════════════════════════════════════════════════════════════════════════════════

def _rule(rule_id, conditions, *, action="reject", reject_text="отказ", enabled=1,
          paused_reason=None):
    return {
        "id": rule_id, "name": None, "city": None, "tracks": ["full"],
        "conditions": conditions, "action": action, "reject_text": reject_text,
        "enabled": enabled, "paused_reason": paused_reason,
    }


# (answers, condition, birth_date, forum_date, expect_fires) — один оператор каждого вида.
_OPERATOR_CASES = [
    ("in — курс совпал",
     {"course": "1"}, {"step": "course", "op": "in", "values": ["1", "2"]}, None, None, True),
    ("in — курс не совпал",
     {"course": "3"}, {"step": "course", "op": "in", "values": ["1", "2"]}, None, None, False),
    ("not_in — курс не в списке",
     {"course": "3"}, {"step": "course", "op": "not_in", "values": ["1", "2"]}, None, None, True),
    ("not_in — курс в списке",
     {"course": "1"}, {"step": "course", "op": "not_in", "values": ["1", "2"]}, None, None, False),
    ("multi in — пересечение непусто",
     {"stack": "Python, Java"}, {"step": "stack", "op": "in", "values": ["Python"]}, None, None, True),
    ("multi in — пересечения нет",
     {"stack": "Go, Rust"}, {"step": "stack", "op": "in", "values": ["Python"]}, None, None, False),
    ("lt — возраст меньше порога",
     {"age": "17"}, {"step": "age", "op": "lt", "values": [18]}, None, None, True),
    ("lt — возраст не меньше порога",
     {"age": "20"}, {"step": "age", "op": "lt", "values": [18]}, None, None, False),
    ("gt — курс больше порога",
     {"course": "5+"}, {"step": "course", "op": "gt", "values": [4]}, None, None, True),
    ("between — курс в диапазоне",
     {"course": "3"}, {"step": "course", "op": "between", "values": [2, 4]}, None, None, True),
    ("between — курс вне диапазона",
     {"course": "1"}, {"step": "course", "op": "between", "values": [2, 4]}, None, None, False),
    ("before — дата раньше",
     {"arrival_date": "01.10.2026"},
     {"step": "arrival_date", "op": "before", "values": ["05.10.2026"]}, None, None, True),
    ("after — дата позже",
     {"arrival_date": "10.10.2026"},
     {"step": "arrival_date", "op": "after", "values": ["05.10.2026"]}, None, None, True),
    ("age_on_forum_lt — младше порога на дату форума",
     {}, {"step": "birth_date", "op": "age_on_forum_lt", "values": [18]},
     "01.01.2009", "15.10.2026", True),
    ("age_on_forum_lt — не младше порога",
     {}, {"step": "birth_date", "op": "age_on_forum_lt", "values": [18]},
     "01.01.2000", "15.10.2026", False),
    ("age_on_forum_lt — нет даты рождения -> условие не выполнено",
     {}, {"step": "birth_date", "op": "age_on_forum_lt", "values": [18]}, None, "15.10.2026", False),
    ("filled — заполнено",
     {"expectations": "хочу нетворкинг"}, {"step": "expectations", "op": "filled", "values": []},
     None, None, True),
    ("filled — прочерк считается пустым",
     {"expectations": "-"}, {"step": "expectations", "op": "filled", "values": []}, None, None, False),
    ("empty — не заполнено",
     {"expectations": ""}, {"step": "expectations", "op": "empty", "values": []}, None, None, True),
    ("has_file — резюме файлом",
     {"resume_file_id": "abc123"}, {"step": "resume", "op": "has_file", "values": []},
     None, None, True),
    ("no_file — резюме нет нигде",
     {}, {"step": "resume", "op": "no_file", "values": []}, None, None, True),
    ("no_file — резюме есть ссылкой",
     {"resume_url": "https://example.com/cv.pdf"},
     {"step": "resume", "op": "no_file", "values": []}, None, None, False),
]


@pytest.mark.parametrize("label,answers,cond,birth_date,forum_date,expect_fires", _OPERATOR_CASES,
                          ids=[c[0] for c in _OPERATOR_CASES])
def test_single_operator_cases(label, answers, cond, birth_date, forum_date, expect_fires):
    rules = [_rule(1, [[cond]])]
    result = reg_engine.evaluate_reject_rules(
        answers, rules, birth_date=birth_date, forum_date=forum_date,
    )
    fired = bool(result["reject_rule_ids"])
    assert fired == expect_fires, label


def test_and_within_group_both_must_match():
    conditions = [[
        {"step": "course", "op": "in", "values": ["1", "2"]},
        {"step": "expectations", "op": "filled", "values": []},
    ]]
    rules = [_rule(1, conditions)]
    # Курс совпадает, но expectations пуст — группа целиком (И) не срабатывает.
    result = reg_engine.evaluate_reject_rules({"course": "1", "expectations": ""}, rules)
    assert result["status_override"] is None
    # Обе половины группы истинны — правило срабатывает.
    result2 = reg_engine.evaluate_reject_rules(
        {"course": "1", "expectations": "хочу приехать"}, rules,
    )
    assert result2["status_override"] == "rejected"


def test_or_between_groups_second_group_fires():
    conditions = [
        [{"step": "course", "op": "in", "values": ["1", "2"]}],
        [{"step": "resume", "op": "no_file", "values": []}],
    ]
    rules = [_rule(1, conditions)]
    result = reg_engine.evaluate_reject_rules({"course": "5+"}, rules)  # первая группа ложна
    assert result["status_override"] == "rejected"  # вторая (нет резюме) — истинна


def test_reject_wins_over_flag_and_flag_still_recorded():
    reject_rule = _rule(1, [[{"step": "course", "op": "in", "values": ["1"]}]],
                         action="reject", reject_text="Курс закрыт.")
    flag_rule = _rule(2, [[{"step": "resume", "op": "no_file", "values": []}]],
                       action="flag")
    result = reg_engine.evaluate_reject_rules({"course": "1"}, [reject_rule, flag_rule])
    assert result["status_override"] == "rejected"
    assert result["reject_rule_ids"] == [1]
    assert result["flag_rule_ids"] == [2]


def test_all_triggered_reject_texts_concatenated_in_input_order():
    rule_a = _rule(1, [[{"step": "course", "op": "in", "values": ["1"]}]],
                    reject_text="Текст А")
    rule_b = _rule(2, [[{"step": "resume", "op": "no_file", "values": []}]],
                    reject_text="Текст Б")
    result = reg_engine.evaluate_reject_rules({"course": "1"}, [rule_a, rule_b])
    assert result["reject_texts"] == ["Текст А", "Текст Б"]


def test_masters_not_caught_by_course_rule():
    # D-32: «Магистратура/Аспирантура» — отдельное значение курса, не 1/2 — правило «курс
    # один из: 1, 2» его не ловит, развилка по возрасту тут не нужна.
    rule = _rule(1, [[{"step": "course", "op": "in", "values": ["1", "2"]}]])
    result = reg_engine.evaluate_reject_rules(
        {"course": "Магистратура/Аспирантура", "education_status": "Да, в ВУЗе или колледже"},
        [rule],
    )
    assert result["status_override"] is None
    assert result["reject_rule_ids"] == []


def test_paused_rule_never_fires():
    rule = _rule(1, [[{"step": "course", "op": "in", "values": ["1"]}]],
                  paused_reason="Курс — вопрос выключен")
    result = reg_engine.evaluate_reject_rules({"course": "1"}, [rule])
    assert result["status_override"] is None


def test_disabled_rule_never_fires():
    rule = _rule(1, [[{"step": "course", "op": "in", "values": ["1"]}]], enabled=0)
    result = reg_engine.evaluate_reject_rules({"course": "1"}, [rule])
    assert result["status_override"] is None


def test_empty_conditions_never_fire():
    rule = _rule(1, [])
    result = reg_engine.evaluate_reject_rules({"course": "1"}, [rule])
    assert result["status_override"] is None
    empty_group_rule = _rule(2, [[]])
    result2 = reg_engine.evaluate_reject_rules({"course": "1"}, [empty_group_rule])
    assert result2["status_override"] is None


def test_returning_delegate_evaluated_same_as_new():
    # D-06: у evaluate_reject_rules нет ни параметра, ни ветки про повторную подачу — те же
    # ответы дают тот же результат вне зависимости от prev_season в answers.
    rule = _rule(1, [[{"step": "course", "op": "in", "values": ["1"]}]])
    answers_new = {"course": "1"}
    answers_returning = {"course": "1", "prev_season": "YL 25"}
    result_new = reg_engine.evaluate_reject_rules(answers_new, [rule])
    result_returning = reg_engine.evaluate_reject_rules(answers_returning, [rule])
    assert result_new["status_override"] == result_returning["status_override"] == "rejected"
    params = list(inspect.signature(reg_engine.evaluate_reject_rules).parameters)
    assert "prev_season" not in params


_ALL_TEST_RULES_FOR_APPROVAL_SWEEP = [
    _rule(1, [[{"step": "course", "op": "in", "values": ["1", "2"]}]], action="reject"),
    _rule(2, [[{"step": "resume", "op": "no_file", "values": []}]], action="flag"),
    _rule(3, [[{"step": "age", "op": "lt", "values": [18]}]], action="reject"),
    _rule(4, [], action="reject"),
    _rule(5, [[{"step": "course", "op": "in", "values": ["9"]}]], action="reject", enabled=0),
]


def test_evaluator_never_approves():
    # D-03/T-31-01-02: перебор по разным входам — status_override не принимает "approved" ни
    # при одном наборе правил/ответов.
    sample_answers = [
        {},
        {"course": "1"},
        {"course": "3", "age": "20", "resume_file_id": "x"},
        {"course": "Магистратура/Аспирантура"},
    ]
    for answers in sample_answers:
        result = reg_engine.evaluate_reject_rules(answers, _ALL_TEST_RULES_FOR_APPROVAL_SWEEP)
        assert result["status_override"] in ("rejected", None)
        assert result["status_override"] != "approved"


def test_broken_condition_does_not_raise_and_yields_false():
    # T-31-01-01: заведомо битый values (between без второго элемента) не пробрасывает
    # исключение — условие просто не срабатывает.
    broken_rule = _rule(1, [[{"step": "course", "op": "between", "values": [2]}]])
    result = reg_engine.evaluate_reject_rules({"course": "3"}, [broken_rule])
    assert result["status_override"] is None

    unknown_operator_rule = _rule(2, [[{"step": "course", "op": "not_a_real_operator", "values": []}]])
    result2 = reg_engine.evaluate_reject_rules({"course": "1"}, [unknown_operator_rule])
    assert result2["status_override"] is None

    unknown_step_rule = _rule(3, [[{"step": "no_such_step", "op": "in", "values": ["x"]}]])
    result3 = reg_engine.evaluate_reject_rules({"course": "1"}, [unknown_step_rule])
    assert result3["status_override"] is None
