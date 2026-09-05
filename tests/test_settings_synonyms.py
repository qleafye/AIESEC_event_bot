"""Phase 22 Plan 02 (WEB-SET-02, D-15): сторож карты `settings_synonyms.SETTINGS_SYNONYMS`.

Форма — та же, что `tests/test_settings_groups_c0x.py::test_settings_groups_cover_every_field_key`
(«каждый X из множества A должен встретиться в множестве B, и ничего лишнего в B»),
применённая к синонимам вместо групп.

Множество редактируемых ключей — `settings_ops.editable_keys()` (план 22-01): все ключи
`SETTINGS_SCHEMA` минус `EXCLUDED_GROUPS` (группа `roles` — у неё своя поверхность правки).
"""
from __future__ import annotations

from settings_ops import editable_keys
from settings_schema import SETTINGS_SCHEMA
from settings_synonyms import SETTINGS_SYNONYMS

EDITABLE_KEYS = set(editable_keys())

# Ключи группы `miniapp`, у которых подпись реестра УЖЕ содержит слова, по которым их ищут
# (тексты веб-экрана настроек и «настроек-лайт» Mini App — управляющие тексты интерфейса
# менеджера, написаны обычным русским языком: «Кнопка «Сохранить N изменений»», «Реквизиты» и
# т.п.), плюс сам факт, что это единственная группа, где почти всё самоописательно — на неё
# указывают Task 2/3 22-02-PLAN.md явно (единственное исключение — `miniapp_enabled`, у него в
# подписи английский термин «Mini App», а не «приложение»/«мини-апп», которыми его назовёт
# менеджер, поэтому он покрыт явным синонимом в SETTINGS_SYNONYMS).
#
# Группа `reg_prompts` (Phase 25, CITYQ-01): 44 ключа `reg_prompt_<step>`, сгенерированные из
# `settings_schema.REG_PROMPT_STEPS` — подпись каждого дословно «✏️ Текст: <подпись вопроса>»,
# т.е. уже содержит те же слова, по которым менеджер ищет сам вопрос (например «✏️ Текст:
# 🎂 Возраст» находится по «возраст»). Ручные синонимы задублировали бы 44 записи, которые
# reg_q_* уже покрывает в SETTINGS_SYNONYMS.
SEARCH_SELF_DESCRIBING = {
    k for k, v in SETTINGS_SCHEMA.items()
    if (v.get("group") == "miniapp" and k != "miniapp_enabled")
    or v.get("group") == "reg_prompts"
}
SEARCH_SELF_DESCRIBING_REASON = (
    "подпись ключа сама содержит слова, по которым его ищут — управляющий текст интерфейса "
    "менеджера (Mini App-экран настроек и «настроек-лайт»), написан обычным русским языком, "
    "либо (группа reg_prompts) подпись дословно повторяет подпись вопроса анкеты"
)


def test_every_editable_key_covered_by_synonyms_or_self_describing():
    """Покрытие (D-15а): каждый редактируемый ключ либо в SETTINGS_SYNONYMS (≥2 синонима),
    либо явно объявлен самоописательным — молчаливых дыр в поиске нет."""
    covered = set(SETTINGS_SYNONYMS) | SEARCH_SELF_DESCRIBING
    missing = sorted(EDITABLE_KEYS - covered)
    assert not missing, f"без синонимов и не самоописательные: {missing}"


def test_synonym_and_self_describing_sets_do_not_overlap():
    overlap = sorted(set(SETTINGS_SYNONYMS) & SEARCH_SELF_DESCRIBING)
    assert not overlap, f"ключ и в SETTINGS_SYNONYMS, и в SEARCH_SELF_DESCRIBING: {overlap}"


def test_three_sets_sum_to_exactly_editable_keys():
    """Сумма SETTINGS_SYNONYMS + SEARCH_SELF_DESCRIBING == editable_keys() — без молчаливо
    пропущенных ключей и без лишних (ключей вне editable_keys в картах быть не должно)."""
    union = set(SETTINGS_SYNONYMS) | SEARCH_SELF_DESCRIBING
    assert union == EDITABLE_KEYS
    assert len(SETTINGS_SYNONYMS) + len(SEARCH_SELF_DESCRIBING) == len(EDITABLE_KEYS)


def test_every_synonym_key_has_at_least_two_synonyms():
    short = {k: v for k, v in SETTINGS_SYNONYMS.items() if len(v) < 2}
    assert not short, f"меньше 2 синонимов: {sorted(short)}"


def test_no_phantom_keys_in_synonyms_map():
    """Дрейф: ни одного выдуманного ключа — при добавлении нового ключа в реестр без записи
    в карте (или наоборот) тест краснеет и называет расхождение поимённо."""
    phantom = sorted(k for k in SETTINGS_SYNONYMS if k not in SETTINGS_SCHEMA)
    assert not phantom, f"ключей нет в SETTINGS_SCHEMA: {phantom}"
    phantom_self = sorted(k for k in SEARCH_SELF_DESCRIBING if k not in SETTINGS_SCHEMA)
    assert not phantom_self, f"самоописательных ключей нет в SETTINGS_SCHEMA: {phantom_self}"


def test_synonym_does_not_duplicate_registry_label():
    """Синоним добавляет слово, которого нет в подписи, а не переформулирует её."""
    dupes = [
        key for key, terms in SETTINGS_SYNONYMS.items()
        if any(t.strip().lower() == SETTINGS_SCHEMA[key]["label"].strip().lower() for t in terms)
    ]
    assert not dupes, f"синоним дублирует подпись: {dupes}"


def test_synonym_hygiene_lowercase_no_empty_no_dup_no_key_codes():
    """Гигиена: строки в нижнем регистре, без пустых, без дублей внутри ключа, без кодов
    ключей («_» в синониме запрещён — менеджер этих строк не видит, но ищет ими, CLAUDE.md)."""
    bad_case = {
        k: v for k, v in SETTINGS_SYNONYMS.items()
        if any(t != t.lower() for t in v)
    }
    assert not bad_case, f"не нижний регистр: {sorted(bad_case)}"

    bad_empty = {
        k: v for k, v in SETTINGS_SYNONYMS.items()
        if any(not t.strip() for t in v)
    }
    assert not bad_empty, f"пустые синонимы: {sorted(bad_empty)}"

    bad_underscore = {
        k: v for k, v in SETTINGS_SYNONYMS.items()
        if any("_" in t for t in v)
    }
    assert not bad_underscore, f"код ключа утёк в синоним: {sorted(bad_underscore)}"

    bad_dup_within = {
        k: v for k, v in SETTINGS_SYNONYMS.items()
        if len(v) != len(set(v))
    }
    assert not bad_dup_within, f"дубли внутри ключа: {sorted(bad_dup_within)}"


def test_settings_synonyms_module_has_no_handlers_import():
    """Docstring-контракт: `settings_synonyms.py` не импортирует ничего из `handlers/` —
    он живёт в корне рядом с `settings_schema.py`, до aiogram-слоя (D-15)."""
    import inspect

    import settings_synonyms

    src = inspect.getsource(settings_synonyms)
    assert "handlers" not in src


def test_reg_questions_synonyms_are_short_topic_words():
    """Task 2: 43 тумблера вопросов — синонимы это тема вопроса в 1-2 словах, подпись и так
    человеческая (см. пример плана: «аллергии»/«еда»; «общежитие»/«жильё»)."""
    reg_q_keys = [k for k in SETTINGS_SCHEMA if k.startswith("reg_q_")]
    assert len(reg_q_keys) == 43
    for key in reg_q_keys:
        assert len(SETTINGS_SYNONYMS.get(key, [])) >= 2, key
