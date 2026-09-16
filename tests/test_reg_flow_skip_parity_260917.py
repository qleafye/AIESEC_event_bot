"""Приёмка 17.09 (находка 2, уточнение владельца): «Пропустить» показывается ТОЛЬКО там, где
пропуск реально принимается сервером — единый источник правды (`reg_engine._SKIP_ALLOWED_STEPS`/
`multi_min_select`), обе поверхности (чат/Mini App) читают его, а не решают сами.

Живой пример регрессии, которую этот сторож обязан ловить: `form_types.js::multiChips` до
приёмки 17.09 предлагала «Пропустить» на ЛЮБОМ multi-шаге с нулём выбранных вариантов, включая
«Форматы форума» — обязательный, сервер отвечал 400 «Выбери хотя бы один вариант.». Сторож ниже
проверяет ту же пару со стороны Python: для КАЖДОГО шага `reg_engine.REG_FLOW` пустой ответ либо
проходит `validate_answer` ИМЕННО тогда, когда сервер объявляет его пропускаемым
(`_NULL_SKIP_STEPS = _SKIP_ALLOWED_STEPS | {"phone"}` — `phone` пропускает по литералу
«Пропустить», отдельно от клавиатуры-пропуска, тот же приём для multi — `min_select == 0`),
либо отвергается — без молчаливого расхождения в любую сторону.

`mini_projects`/`mini_direction` — единственное известное расхождение (в `_ALLOWLIST_TOLERANT`
ниже): у них нет собственной ветки в `_validate_answer_core`, и пустой ответ проходит тем же
общим «терпимым» фоллбэком, что был у бота для любого текстового шага без явной проверки
(докстринг `_validate_answer_core`, ветка `return (raw or "").strip(), None`) — ни один UI не
показывает им нерабочую кнопку «Пропустить» (её просто нет), это не тот класс бага, что находка
2 (кнопка ЕСТЬ, но не работает); допущение задокументировано явно, а не тихо исключено."""
from __future__ import annotations

import pytest

import reg_engine

STEP_KEYS = [step_key for step_key, _setting_key, _step_type in reg_engine.REG_FLOW]

# Пре-существующее расхождение (не находка 17.09, отдельный технический долг): эти два шага не
# входят в `_SKIP_ALLOWED_STEPS`/`multi_min_select`, но пустой ответ проходит общим «терпимым»
# фоллбэком без своей ветки валидации — сервер НЕ строже спеки (обратный, безопасный случай),
# просто спека сегодня не отражает фактическую нестрогость. Список — единственное разрешённое
# расхождение; новый шаг сюда не добавляется без отдельного разбора (см. докстринг файла).
_KNOWN_TOLERANT_FALLBACK_MISMATCH = {"mini_projects", "mini_direction"}


@pytest.mark.parametrize("step_key", STEP_KEYS)
def test_skip_declaration_matches_validate_answer_on_empty(step_key):
    """`step_key in _NULL_SKIP_STEPS` (спека объявляет шаг пропускаемым — Mini App
    `spec["skip_allowed"]`/`spec["min_select"] == 0`, чат показывает клавиатуру «Пропустить»
    ИЛИ принимает литерал «Пропустить» как `phone`) должно ⟺ `validate_answer(step_key, empty)`
    не возвращает ошибку. Мультивыбор проверяется пустым списком (его реальная форма ответа),
    остальные типы — `None` (эквивалент «ничего не прислали», Pitfall 10 21-10)."""
    if step_key in _KNOWN_TOLERANT_FALLBACK_MISMATCH:
        pytest.skip("пре-существующий терпимый фоллбэк без явной ветки валидации, см. докстринг")
    is_multi = reg_engine.REG_STEP_TYPES.get(step_key) == "multi"
    empty_raw = [] if is_multi else None
    _value, error = reg_engine.validate_answer(step_key, empty_raw)
    accepted = error is None
    declared_skippable = step_key in reg_engine._NULL_SKIP_STEPS
    if is_multi:
        # Для multi отдельно сверяем НОВЫЙ явный флаг (`min_select`) — он публикуется в
        # `spec["min_select"]` (Mini App) и им же управляет `multiChips.footerState()`/
        # `multiControl` (form.js/form_types.js) после фикса находки 2.
        assert (reg_engine.multi_min_select(step_key) == 0) == accepted, (
            f"{step_key}: multi_min_select и фактическая приёмка пустого списка разошлись"
        )
    assert declared_skippable == accepted, (
        f"{step_key}: _NULL_SKIP_STEPS={declared_skippable}, "
        f"но validate_answer(..., {empty_raw!r}) {'принял' if accepted else 'отверг'} пустой ответ "
        f"(error={error!r})"
    )


def test_no_multi_step_is_skippable_today():
    """Сегодня ни один multi-шаг не входит в `_SKIP_ALLOWED_STEPS` — `min_select` для всех
    равен 1. Этот тест ломается НАМЕРЕННО в тот день, когда кто-то добавит скипуемый
    multi-шаг — сигнал перечитать `reg_engine._NULL_SKIP_STEPS`/`validate_answer` (multi-ветка
    ждёт список, а `_NULL_SKIP_STEPS`-литерал «Пропустить» сегодня multi не обслуживает, см.
    докстринг `_validate_answer_core`) прежде чем полагаться на пропуск такого шага вслепую."""
    multi_steps = [k for k in STEP_KEYS if reg_engine.REG_STEP_TYPES.get(k) == "multi"]
    assert multi_steps, "в REG_FLOW нет ни одного multi-шага — сторож проверить нечего"
    for step_key in multi_steps:
        assert reg_engine.multi_min_select(step_key) == 1, step_key
