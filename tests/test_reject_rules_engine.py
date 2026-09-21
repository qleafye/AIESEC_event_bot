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

