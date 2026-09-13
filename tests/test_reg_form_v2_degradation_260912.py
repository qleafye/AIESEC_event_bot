"""Phase 30 (30-08, задача 1, A2-08): матрица деградации девяти тумблеров «📝 Анкета» × семи
канонических типов шага + три экрана (обзор перед отправкой / статус заявки / слой настроек в
шапке), которые тумблерами управляются, но типом шага не являются.

Обещание A2-08 («каждый элемент — тумблером, дефолт как сегодня») проверяется здесь МАШИНОЙ —
через РЕАЛЬНЫЕ функции (`reg_engine.degrade_kind`/`form_v2_flags`/`form_spec`), а не через
копию таблиц 30-UI-SPEC.md в этом файле (иначе тест был бы тавтологией: копия правил против
самих правил). Источник ожидаемых значений в именованных кейсах ниже — сама спека
(`30-UI-SPEC.md` § «По типу шага» → «Деградация» каждого из семи типов).

НЕ дублирует уже существующее покрытие:
- `tests/test_reg_step_type_v2_260912.py` — покрытие REG_FLOW (каждый шаг = один из семи
  типов), паритет проекций (`CHAT_PROJECTION`/`APP_PROJECTION`), спека composite/`spec.lookup`.
- `tests/test_reg_types_chat_260912.py` — поведение lookup/composite/repeatable В ЧАТЕ на
  конкретных сценариях (не таблица тумблеров).
- `tests/test_miniapp_form_types_js_260912.py` — рендер `form_types.js` (chips/search/link/
  composite/repeatable) на уровне JS-компонентов.

Этот файл — единственное место, где таблица деградации проверяется ЦЕЛИКОМ (9×7 + три экрана
+ обязательный «дефолт = легаси» + обязательный «ни одна комбинация не роняет функцию»).
"""
import asyncio
import itertools

import pytest

from config import config
from database.db import init_db, set_setting

from reg_engine import FORM_V2_TOGGLE_KEYS, degrade_kind, form_spec, form_v2_flags

CANONICAL_KINDS = ("select", "lookup", "composite", "link", "multi", "repeatable", "text")
CLOSED_KIND_SET = frozenset({"legacy", *CANONICAL_KINDS})


def _ready(tmp_path, name="reg_form_v2_degradation.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(init_db())


def _flags(**overrides) -> dict[str, bool]:
    """Девять тумблеров, все `False` по умолчанию (литерал, НЕ чтение из реестра — так тест
    ловит рассинхрон, если кто-то сменит дефолт в `SETTINGS_SCHEMA`, не пересмотрев эту
    матрицу), `overrides` точечно включают нужные. Тот же плоский словарь, что реально
    возвращает `form_v2_flags()`."""
    base = {name: False for name in FORM_V2_TOGGLE_KEYS}
    base.update(overrides)
    return base


ALL_ON = _flags(**{name: True for name in FORM_V2_TOGGLE_KEYS})


# ── (1) Обязательный кейс: все девять тумблеров в дефолте → "legacy" для всех семи типов ────

@pytest.mark.parametrize("kind", CANONICAL_KINDS)
def test_all_toggles_default_gives_legacy_for_every_kind(kind):
    """Падает, если у любого из девяти ключей сменили дефолт (`SETTINGS_SCHEMA` `default`) —
    `_flags()` строится из литерала `False`, не из реестра, поэтому смена дефолта сама по себе
    этот тест не подвинет: она обязана пройти через явный пересмотр матрицы здесь."""
    assert degrade_kind(kind, _flags()) == "legacy"


def test_all_toggles_default_gives_legacy_via_real_db_defaults(tmp_path):
    """То же самое, но через РЕАЛЬНЫЙ путь чтения (`form_v2_flags()` из пустого реестра свежей
    БД), не руками собранный словарь — ловит рассинхрон между фактическим дефолтом
    `SETTINGS_SCHEMA` и литералом `_flags()` выше."""
    _ready(tmp_path)
    flags = asyncio.run(form_v2_flags(None))
    assert all(v is False for v in flags.values()), flags
    for kind in CANONICAL_KINDS:
        assert degrade_kind(kind, flags) == "legacy"


# ── (2) Обязательный кейс: мастер включён, остальные восемь в дефолте → каждый тип рисуется
# своим новым видом, КРОМЕ тех, у кого собственный тумблер выключен (30-UI-SPEC.md таблицы) ──

MASTER_ONLY = _flags(v2_enabled=True)

MASTER_ONLY_EXPECTED = {
    "select": "select",       # нет отдельного тумблера — управляется только master switch
    "lookup": "text",         # chips=False И lookup_search=False → «оба выключены» → text
    "composite": "legacy",    # edu_card=False → архитектура шага целиком легаси, не «урезанная карточка»
    "link": "link",           # нет отдельного тумблера
    "multi": "text",          # chips=False → фактический откат к text (лимит остаётся только подсказкой)
    "repeatable": "text",     # repeatable=False → один блок максимум, сегодняшнее поведение
    "text": "text",           # нет отдельного тумблера
}


@pytest.mark.parametrize("kind,expected", sorted(MASTER_ONLY_EXPECTED.items()))
def test_master_on_others_default_off_matches_ui_spec_degradation_table(kind, expected):
    assert degrade_kind(kind, MASTER_ONLY) == expected


# ── (3) Именованные кейсы «один тумблер включает/выключает свой тип» ────────────────────────
# (имя_кейса) -> (тумблер_override, тип_шага, ожидаемый_результат) — дословно из таблиц
# 30-UI-SPEC.md § «По типу шага» → «Деградация», не копия логики `degrade_kind`.

NAMED_CASES = {
    "lookup_chips_only_enables_lookup": (dict(chips=True), "lookup", "lookup"),
    "lookup_search_only_enables_lookup": (dict(lookup_search=True), "lookup", "lookup"),
    "lookup_neither_chips_nor_search_degrades_to_text": (dict(), "lookup", "text"),
    "lookup_both_chips_and_search_stays_lookup": (dict(chips=True, lookup_search=True), "lookup", "lookup"),
    "multi_chips_enables_multi": (dict(chips=True), "multi", "multi"),
    "multi_without_chips_degrades_to_text": (dict(), "multi", "text"),
    "composite_edu_card_enables_composite": (dict(edu_card=True), "composite", "composite"),
    "composite_without_edu_card_is_legacy": (dict(), "composite", "legacy"),
    "repeatable_toggle_enables_repeatable": (dict(repeatable=True), "repeatable", "repeatable"),
    "repeatable_without_toggle_degrades_to_text": (dict(), "repeatable", "text"),
    "select_has_no_sub_toggle_always_select_when_master_on": (dict(), "select", "select"),
    "link_has_no_sub_toggle_always_link_when_master_on": (dict(), "link", "link"),
    "text_has_no_sub_toggle_always_text_when_master_on": (dict(), "text", "text"),
}


@pytest.mark.parametrize("case_name,params", sorted(NAMED_CASES.items()))
def test_named_degradation_case(case_name, params):
    overrides, kind, expected = params
    flags = _flags(v2_enabled=True, **overrides)
    assert degrade_kind(kind, flags) == expected, case_name


# ── (4) Тумблеры-«не про тип шага»: limit_counter/status_screen/header_settings/haptics — это
# тумблеры ТРЁХ ЭКРАНОВ (счётчик в заголовке multi/статус/шапка), они НЕ входят в правила
# `degrade_kind` ни одного канонического типа (см. п.6 ниже — экраны проверяются отдельно через
# `form_v2_flags`/`form_spec`, не через `degrade_kind`, у которого для них просто нет ветки).

SCREEN_ONLY_TOGGLES = ("limit_counter", "status_screen", "header_settings", "haptics")


@pytest.mark.parametrize("toggle_name", SCREEN_ONLY_TOGGLES)
def test_screen_only_toggle_does_not_change_any_kind_degradation(toggle_name):
    flags = dict(ALL_ON)
    flags[toggle_name] = False
    for kind in CANONICAL_KINDS:
        assert degrade_kind(kind, flags) == degrade_kind(kind, ALL_ON), (toggle_name, kind)


# ── (5) Обязательный кейс: НИ ОДНА комбинация девяти тумблеров не бросает исключение и не
# отдаёт значение вне закрытого словаря — полный перебор 2⁹ × 7 = 3584 вызовов чистой функции
# (`degrade_kind` не ходит в БД, дёшево гонять исчерпывающе, не выборочно).

def test_no_combination_of_nine_toggles_raises_or_escapes_closed_set():
    names = FORM_V2_TOGGLE_KEYS
    for combo in itertools.product((False, True), repeat=len(names)):
        flags = dict(zip(names, combo))
        for kind in CANONICAL_KINDS:
            result = degrade_kind(kind, flags)
            assert result in CLOSED_KIND_SET, (flags, kind, result)


# ── (6) Три экрана: обзор / статус / шапка — деградация через РЕАЛЬНЫЙ form_v2_flags()/
# form_spec(), а не через degrade_kind (у экранов нет "kind") ───────────────────────────────

def test_review_screen_gate_follows_master_toggle_via_real_form_spec(tmp_path):
    """`screens/form.js::drawReview()` гейтится `specs[0].flags.v2_enabled` (реальный код,
    30-05-SUMMARY.md «isV2Form») — здесь проверяем публикуемый КОНТРАКТ (что клиент реально
    получит в спеке), не сам JS-рендер (тот уже покрыт `tests/test_miniapp_form_entry_js.py`)."""
    _ready(tmp_path)
    form_off = asyncio.run(form_spec({}, participant_type="full"))
    assert form_off["steps"][0]["flags"]["v2_enabled"] is False

    asyncio.run(set_setting("reg_form_v2_enabled", "on"))
    form_on = asyncio.run(form_spec({}, participant_type="full"))
    assert form_on["steps"][0]["flags"]["v2_enabled"] is True


def test_status_screen_toggle_reflected_in_form_v2_flags(tmp_path):
    """`miniapp/routers/hub.py::hub_status()` гейтит ВЕСЬ новый контракт статуса одним булевым
    `status_screen_enabled` (30-05-SUMMARY.md) — источник этого булева ровно здесь."""
    _ready(tmp_path)
    flags_off = asyncio.run(form_v2_flags(None))
    assert flags_off["status_screen"] is False

    asyncio.run(set_setting("reg_form_status_screen", "on"))
    flags_on = asyncio.run(form_v2_flags(None))
    assert flags_on["status_screen"] is True


def test_header_settings_toggle_reflected_in_form_v2_flags(tmp_path):
    """`screens/form.js::buildHeaderSettingsGear(spec.flags)` — шестерёнка появляется только
    при `flags.header_settings` (30-05-SUMMARY.md)."""
    _ready(tmp_path)
    flags_off = asyncio.run(form_v2_flags(None))
    assert flags_off["header_settings"] is False

    asyncio.run(set_setting("reg_form_header_settings", "on"))
    flags_on = asyncio.run(form_v2_flags(None))
    assert flags_on["header_settings"] is True


# ── (7) Сводный тест: реестр типов не оставил проекции-заглушки ────────────────────────────

def test_pending_projections_is_empty():
    """Фаза не оставила ни одного типа без обеих проекций — план 30-06 уже опустошил
    `PENDING_PROJECTIONS` (`reg_engine.py`), это финальная приёмочная проверка замка фазы."""
    import tests.test_reg_step_type_v2_260912 as sibling

    assert sibling.PENDING_PROJECTIONS == {}
