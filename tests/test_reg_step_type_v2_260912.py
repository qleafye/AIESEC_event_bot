"""Phase 30 (30-01, A2-01) — сторож реестра типов «Анкета 2.0»: покрытие (каждый шаг REG_FLOW
получает ровно один из семи канонических типов), деградация (таблицы «Деградация» из
30-UI-SPEC.md § «По типу шага», дословно — degrade_kind обязана быть ЕДИНСТВЕННЫМ местом с
этими правилами, T-30-02) и паритет проекций (у каждого типа обязаны быть ОБЕ поверхности —
чат и Mini App, либо явная временная заглушка `PENDING_PROJECTIONS` с указанием, какой план её
снимает).

Паритет проверяется по ЯВНОЙ таблице (`reg_engine.CHAT_PROJECTION`/`APP_PROJECTION`), не по
grep произвольного токена — импорт чат-модуля идёт через `importlib.import_module` (реальная
проверка «модуль существует и импортируется без ошибок»), а не текстовый поиск имени."""
import importlib
import os

import pytest

from reg_engine import (
    APP_PROJECTION,
    CHAT_PROJECTION,
    PENDING_PROJECTIONS,
    REG_FLOW,
    composite_group_of,
    degrade_kind,
    step_type_v2,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANONICAL_KINDS = ("select", "lookup", "composite", "link", "multi", "repeatable", "text")


# ── (1) Покрытие: каждый шаг REG_FLOW получает ровно один канонический тип ─────────────────

def test_every_reg_flow_step_has_exactly_one_canonical_kind():
    for step_key, _setting_key, _step_type in REG_FLOW:
        kind = step_type_v2(step_key)
        assert kind in CANONICAL_KINDS, f"{step_key}: неизвестный тип {kind!r}"


@pytest.mark.parametrize(
    "step_key,expected_kind",
    [
        ("university", "lookup"),
        ("city", "lookup"),
        ("vk", "link"),
        ("resume_link", "link"),
        ("mini_portfolio", "repeatable"),
        ("phone", "text"),
    ],
)
def test_step_kind_overrides_match_30_research_pattern_1(step_key, expected_kind):
    assert step_type_v2(step_key) == expected_kind


def test_composite_group_education_has_four_steps_and_kind_composite():
    education_steps = ["education_status", "course", "study_field"]
    for step_key in education_steps:
        assert composite_group_of(step_key) == "education"
        assert step_type_v2(step_key) == "composite"
    # university стоит и в группе, и в override — override сильнее (30-01-PLAN.md task 1):
    # тип шага "university" остаётся lookup, хотя он часть карточки "Образование".
    assert composite_group_of("university") == "education"
    assert step_type_v2("university") == "lookup"


def test_no_unknown_kind_values_across_whole_reg_flow():
    seen = {step_type_v2(step_key) for step_key, _sk, _t in REG_FLOW}
    assert seen <= set(CANONICAL_KINDS)


# ── (2) Деградация — таблицы «Деградация» 30-UI-SPEC.md § «По типу шага», дословно ─────────

_ALL_TOGGLES_ON = {
    "v2_enabled": True, "chips": True, "lookup_search": True, "edu_card": True,
    "repeatable": True, "limit_counter": True, "status_screen": True,
    "header_settings": True, "haptics": True,
}


def _flags(**overrides) -> dict[str, bool]:
    flags = dict(_ALL_TOGGLES_ON)
    flags.update(overrides)
    return flags


# (тумблер(и) выключены, тип, ожидаемый результат degrade_kind) — построено дословно по
# таблицам «Деградация» каждого типа в 30-UI-SPEC.md § «По типу шага».
_DEGRADATION_CASES = [
    # §1 select — единственная деградация через master switch.
    ("master switch off -> select", _flags(v2_enabled=False), "select", "legacy"),
    ("select stays select with v2 on", _flags(), "select", "select"),
    # §2 lookup — цепочка: chips off -> lookup остаётся lookup (поиск ещё жив).
    ("lookup: chips off, search on", _flags(chips=False), "lookup", "lookup"),
    ("lookup: chips on, search off", _flags(lookup_search=False), "lookup", "lookup"),
    ("lookup: both off -> text", _flags(chips=False, lookup_search=False), "lookup", "text"),
    ("lookup: both on", _flags(), "lookup", "lookup"),
    # §3 composite — edu_card off -> legacy (архитектура шага меняется целиком, не визуал).
    ("composite: edu_card off -> legacy", _flags(edu_card=False), "composite", "legacy"),
    ("composite: edu_card on", _flags(), "composite", "composite"),
    # §4 link — деградация только через master switch.
    ("master switch off -> link", _flags(v2_enabled=False), "link", "legacy"),
    ("link stays link with v2 on", _flags(), "link", "link"),
    # §5 multi — chips off -> откат к text (limit_counter не влияет на сам kind).
    ("multi: chips off -> text", _flags(chips=False), "multi", "text"),
    ("multi: limit_counter off, chips on -> multi", _flags(limit_counter=False), "multi", "multi"),
    ("multi: both off -> text", _flags(chips=False, limit_counter=False), "multi", "text"),
    ("multi stays multi with v2 on", _flags(), "multi", "multi"),
    # §6 repeatable — repeatable off -> один блок максимум (откат к text).
    ("repeatable: off -> text", _flags(repeatable=False), "repeatable", "text"),
    ("repeatable: on", _flags(), "repeatable", "repeatable"),
    # §7 text/phone — деградация только через master switch.
    ("master switch off -> text", _flags(v2_enabled=False), "text", "legacy"),
    ("text stays text with v2 on", _flags(), "text", "text"),
]


@pytest.mark.parametrize("case_name,flags,kind,expected", _DEGRADATION_CASES, ids=[c[0] for c in _DEGRADATION_CASES])
def test_degrade_kind_matches_ui_spec_degradation_tables(case_name, flags, kind, expected):
    assert degrade_kind(kind, flags) == expected, case_name


def test_all_nine_toggles_default_off_degrades_every_canonical_kind_to_legacy():
    """A2-08/D-06 acceptance: делегат не видит НИЧЕГО нового, пока менеджер явно не включил
    хотя бы мастер-тумблер — при девяти дефолтных `False` любой тип отдаёт "legacy"."""
    all_off = {name: False for name in _ALL_TOGGLES_ON}
    for kind in CANONICAL_KINDS:
        assert degrade_kind(kind, all_off) == "legacy", kind


# ── (3) Паритет проекций — явная таблица, importlib, файл на диске, литеральный case ───────

def test_projection_tables_cover_exactly_seven_canonical_kinds():
    assert set(CHAT_PROJECTION) == set(CANONICAL_KINDS)
    assert set(APP_PROJECTION) == set(CANONICAL_KINDS)


def test_pending_projections_only_names_canonical_kinds_with_a_non_empty_reason():
    assert set(PENDING_PROJECTIONS) <= set(CANONICAL_KINDS)
    for kind, reason in PENDING_PROJECTIONS.items():
        assert isinstance(reason, str) and reason.strip(), f"{kind}: пустая причина в PENDING_PROJECTIONS"
        assert "30-0" in reason, f"{kind}: причина обязана называть план, который её снимает — {reason!r}"


def test_dedicated_seam_kinds_do_not_point_to_the_chat_aggregator():
    """lookup/composite/repeatable не существовали ДО этой фазы — их чат-проекция обязана быть
    выделенным швом (handlers/reg_types_*.py), а не старым агрегатором handlers.registration."""
    for kind in ("lookup", "composite", "repeatable"):
        assert CHAT_PROJECTION[kind] != "handlers.registration", kind


def test_step_type_v2_has_both_projections():
    """(а) обе таблицы покрывают ровно семь типов — уже отдельным тестом выше, здесь для
    целостности одного сторожа. (б) для типов НЕ в PENDING_PROJECTIONS чат-модуль реально
    импортируется (importlib), файл фронт-проекции существует на диске. (в) выделенные швы не
    указывают на агрегатор. (г) в файле фронт-проекции есть литеральная ветка `case "<тип>"`."""
    assert set(CHAT_PROJECTION) == set(CANONICAL_KINDS)
    assert set(APP_PROJECTION) == set(CANONICAL_KINDS)

    for kind in CANONICAL_KINDS:
        if kind in PENDING_PROJECTIONS:
            continue  # временная заглушка — план из PENDING_PROJECTIONS[kind] её снимет
        chat_module = CHAT_PROJECTION[kind]
        importlib.import_module(chat_module)  # реальный импорт, не grep имени

        app_file = APP_PROJECTION[kind]
        app_path = os.path.join(REPO_ROOT, *app_file.split("/"))
        assert os.path.isfile(app_path), f"{kind}: {app_file} не существует на диске"

        if kind in ("lookup", "composite", "repeatable"):
            assert chat_module != "handlers.registration", kind

        with open(app_path, encoding="utf-8") as fh:
            content = fh.read()
        assert f'case "{kind}"' in content, f"{kind}: нет литеральной ветки case в {app_file}"


def test_pending_projections_is_empty_only_when_every_module_and_case_actually_exist():
    """Обратная сторона теста выше: если PENDING_PROJECTIONS вдруг опустела раньше времени
    (план забыл оставить заглушку), сторож обязан упасть тут же, а не молчать — проверяем ту
    же тройку условий для ВСЕХ семи типов, что и test_step_type_v2_has_both_projections делает
    только для непомеченных."""
    for kind in CANONICAL_KINDS:
        if kind not in PENDING_PROJECTIONS:
            chat_module = CHAT_PROJECTION[kind]
            importlib.import_module(chat_module)
            app_path = os.path.join(REPO_ROOT, *APP_PROJECTION[kind].split("/"))
            assert os.path.isfile(app_path)
