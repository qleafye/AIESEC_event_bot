"""Phase 30 (30-01, A2-01) — сторож реестра типов «Анкета 2.0»: покрытие (каждый шаг REG_FLOW
получает ровно один из семи канонических типов), деградация (таблицы «Деградация» из
30-UI-SPEC.md § «По типу шага», дословно — degrade_kind обязана быть ЕДИНСТВЕННЫМ местом с
этими правилами, T-30-02) и паритет проекций (у каждого типа обязаны быть ОБЕ поверхности —
чат и Mini App, либо явная временная заглушка `PENDING_PROJECTIONS` с указанием, какой план её
снимает).

Паритет проверяется по ЯВНОЙ таблице (`reg_engine.CHAT_PROJECTION`/`APP_PROJECTION`), не по
grep произвольного токена — импорт чат-модуля идёт через `importlib.import_module` (реальная
проверка «модуль существует и импортируется без ошибок»), а не текстовый поиск имени."""
import asyncio
import importlib
import os

import pytest

from config import config
from database.db import init_db, set_setting

from reg_engine import (
    APP_PROJECTION,
    CHAT_PROJECTION,
    FORM_V2_TOGGLE_KEYS,
    PENDING_PROJECTIONS,
    REG_FLOW,
    composite_group_of,
    composite_parts,
    degrade_kind,
    form_spec,
    step_spec,
    step_type_v2,
)


def _ready(tmp_path, name="reg_step_type_v2.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(init_db())


def _all_form_v2_toggles_on():
    """Включает девять тумблеров «📝 Анкета» в реестре — `form_spec()` сам читает их из БД
    (`form_v2_flags`), в отличие от прямых вызовов `step_spec(flags=_ALL_ON_FLAGS)` выше."""
    for name in FORM_V2_TOGGLE_KEYS:
        asyncio.run(set_setting(f"reg_form_{name}", "on"))

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


# ── (4) composite «Образование»: карточка из включённых частей (30-04, задача 2/4, A2-04) ──

_ALL_ON_FLAGS = {
    "v2_enabled": True, "chips": True, "lookup_search": True, "edu_card": True,
    "repeatable": True, "limit_counter": True, "status_screen": True,
    "header_settings": True, "haptics": True,
}


def test_composite_parts_with_only_toggle_enabled_gives_single_part_card(tmp_path):
    """30-CONTEXT.md реш. 2: «карточка всегда, из включённых частей» — событие, у которого
    включён только вопрос «Учишься сейчас?», получает КАРТОЧКУ из одной части, а не откат к
    одиночному старому шагу (не `degrade_kind` -> `"legacy"`, а composite с `len(parts) == 1`)."""
    _ready(tmp_path)
    asyncio.run(set_setting("reg_q_university", "off"))
    asyncio.run(set_setting("reg_q_course", "off"))
    asyncio.run(set_setting("reg_q_study_field", "off"))

    parts = asyncio.run(composite_parts("education"))
    assert parts == ["education_status"]

    spec = asyncio.run(step_spec("education_status", None, None, flags=_ALL_ON_FLAGS))
    assert spec["degraded_kind"] == "composite"
    assert spec["composite"]["parts"][0]["key"] == "education_status"
    assert len(spec["composite"]["parts"]) == 1


def test_composite_parts_with_all_four_toggles_on_gives_full_card_in_reg_flow_order(tmp_path):
    _ready(tmp_path)
    parts = asyncio.run(composite_parts("education"))
    assert parts == ["education_status", "course", "university", "study_field"]


def test_composite_sub_spec_university_keeps_its_own_lookup_kind(tmp_path):
    """ВУЗ состоит и в группе (`composite_group_of`), и в `_STEP_TYPE_V2_OVERRIDES` — карточка
    не разваливается, деградирует только внутренний контрол (30-UI-SPEC.md §3)."""
    _ready(tmp_path)
    spec = asyncio.run(step_spec("education_status", None, None, flags=_ALL_ON_FLAGS))
    parts_by_key = {p["key"]: p for p in spec["composite"]["parts"]}
    assert parts_by_key["university"]["kind"] == "lookup"
    assert parts_by_key["university"]["degraded_kind"] == "lookup"
    assert "composite" not in parts_by_key["university"]


def test_composite_sub_specs_do_not_recurse_into_their_own_composite_field(tmp_path):
    """Защита от бесконечной рекурсии (`step_spec(_in_composite=True)`, docstring reg_engine.py)
    — course/study_field/education_status сами имеют `kind == "composite"`, но их под-спеки
    внутри карточки НЕ несут собственный `spec["composite"]`."""
    _ready(tmp_path)
    spec = asyncio.run(step_spec("education_status", None, None, flags=_ALL_ON_FLAGS))
    for part in spec["composite"]["parts"]:
        if part["key"] != "university":
            assert "composite" not in part, part["key"]


# ── (5) form_spec подмешивает текущие/прежние значения в composite.parts (план 30-05, задача 0б,
# хвост 30-04-SUMMARY.md «Composite не получает текущие/прошлые значения делегата») ─────────────

def _education_step(form: dict) -> dict:
    return next(s for s in form["steps"] if s["key"] == "education_status")


def test_composite_parts_get_current_answers_from_form_spec(tmp_path):
    """Делегат уже ответил на карточку в этой же сессии — `answers` несёт колонки
    university/course/study_field, части карточки обязаны их увидеть как `part["value"]`, не
    стартовать пустыми (30-04-SUMMARY.md Known Stubs)."""
    _ready(tmp_path)
    _all_form_v2_toggles_on()
    answers = {
        "education_status": "Да, очно", "university": "СПбГЭТУ",
        "course": "3", "study_field": "Информатика",
    }
    form = asyncio.run(form_spec(answers, participant_type="full"))
    parts_by_key = {p["key"]: p for p in _education_step(form)["composite"]["parts"]}
    assert parts_by_key["university"]["value"] == "СПбГЭТУ"
    assert parts_by_key["course"]["value"] == "3"
    assert parts_by_key["study_field"]["value"] == "Информатика"


def test_composite_parts_fall_back_to_prior_when_no_current_answer(tmp_path):
    """Возвращенец без ответа В ЭТОЙ анкете, но с прошлым сезоном (`prior_answers_for`) —
    части карточки получают значение из `prior`, как и обычные (не composite) шаги."""
    _ready(tmp_path)
    _all_form_v2_toggles_on()
    prior = {"university": "МГУ", "course": "2", "study_field": "Экономика"}
    form = asyncio.run(form_spec({}, participant_type="full", prior=prior))
    parts_by_key = {p["key"]: p for p in _education_step(form)["composite"]["parts"]}
    assert parts_by_key["university"]["value"] == "МГУ"
    assert parts_by_key["university"]["prior"]["value"] == "МГУ"
    assert parts_by_key["course"]["value"] == "2"


def test_composite_parts_are_none_without_answer_or_prior(tmp_path):
    """Совсем новый делегат — части карточки НЕ подставляют случайное значение, `value` явно
    `None` (тот же контракт, что у верхнего уровня спеки, не пустая строка молчаливо)."""
    _ready(tmp_path)
    _all_form_v2_toggles_on()
    form = asyncio.run(form_spec({}, participant_type="full"))
    parts_by_key = {p["key"]: p for p in _education_step(form)["composite"]["parts"]}
    assert parts_by_key["university"]["value"] is None
    assert parts_by_key["university"]["prior"] is None


def test_composite_studying_flag_reflects_toggle_answer(tmp_path):
    """`spec["composite"]["studying"]` — сервер решает «учится/не учится» по тому же реестровому
    списку статусов (`is_studying`/`studying_statuses`), что `enabled_steps`/`apply_answers`, а
    не хардкодит `True` (30-04-SUMMARY.md Known Stubs)."""
    _ready(tmp_path)
    _all_form_v2_toggles_on()
    form_not_studying = asyncio.run(form_spec({"education_status": "Уже не учусь"}, participant_type="full"))
    assert _education_step(form_not_studying)["composite"]["studying"] is False

    form_studying = asyncio.run(form_spec({"education_status": "Да, очно"}, participant_type="full"))
    assert _education_step(form_studying)["composite"]["studying"] is True

    form_no_answer = asyncio.run(form_spec({}, participant_type="full"))
    assert _education_step(form_no_answer)["composite"]["studying"] is True


# ── (6) spec["lookup"]: атрибуты списка сверх глобальных тумблеров (30-08, задача A) ────────
# Правило дословно: глобальный `off` -> `off` без похода в атрибут списка; глобальный `on` ->
# решает атрибут конкретного списка (`reg_engine.lookup_render_flags`).

def test_lookup_spec_global_on_list_attribute_off_disables_render_flag(tmp_path):
    _ready(tmp_path)
    asyncio.run(set_setting("university_options_chips_enabled", "off"))
    spec = asyncio.run(step_spec("university", None, None, flags=_ALL_ON_FLAGS))
    assert spec["degraded_kind"] == "lookup"
    assert spec["lookup"] == {"chips_enabled": False, "search_enabled": True}


def test_lookup_spec_global_off_wins_even_if_list_attribute_on(tmp_path):
    """Глобальный тумблер `off` -> `off`, БЕЗ похода в реестр атрибута списка — правило задачи A
    дословно («глобальный off → off»), список тут ни при чём, даже если сам атрибут = «on»
    (дефолт)."""
    _ready(tmp_path)
    flags_no_chips = dict(_ALL_ON_FLAGS, chips=False)
    spec = asyncio.run(step_spec("university", None, None, flags=flags_no_chips))
    assert spec["degraded_kind"] == "lookup"  # lookup_search=True держит kind "lookup"
    assert spec["lookup"] == {"chips_enabled": False, "search_enabled": True}


def test_lookup_spec_per_list_attributes_are_independent_per_step(tmp_path):
    """`university_options`/`city_options` — разные ключи реестра, атрибут одного списка не
    протекает в другой."""
    _ready(tmp_path)
    asyncio.run(set_setting("university_options_search_enabled", "off"))
    uni_spec = asyncio.run(step_spec("university", None, None, flags=_ALL_ON_FLAGS))
    city_spec = asyncio.run(step_spec("city", None, None, flags=_ALL_ON_FLAGS))
    assert uni_spec["lookup"] == {"chips_enabled": True, "search_enabled": False}
    assert city_spec["lookup"] == {"chips_enabled": True, "search_enabled": True}


def test_lookup_spec_absent_for_non_lookup_kind():
    """Узел `spec["lookup"]` публикуется ТОЛЬКО у `degraded_kind == "lookup"` — у прочих типов
    его нет вовсе (не пустой словарь), чтобы фронт не путал «нет узла» с «оба флага false»."""
    spec = asyncio.run(step_spec("alumni_status", None, None, flags=_ALL_ON_FLAGS))
    assert spec["degraded_kind"] == "select"
    assert "lookup" not in spec


def test_lookup_render_flags_defaults_to_on_on_when_v2_toggles_default(tmp_path):
    """Дефолт обоих атрибутов списка — `"on"` (30-07): при включённом мастере и глобальных
    тумблерах чипов/поиска новый рендер lookup сразу получает и чипы, и поиск, без ручной
    настройки менеджера."""
    _ready(tmp_path)
    spec = asyncio.run(step_spec("university", None, None, flags=_ALL_ON_FLAGS))
    assert spec["lookup"] == {"chips_enabled": True, "search_enabled": True}
