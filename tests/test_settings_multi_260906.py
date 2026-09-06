"""Quick 260906-6xe (Task 1): тип реестра `multi` — единственный маппинг код<->подпись для
закрытых наборов (`settings_schema.multi_options/multi_labels/multi_codes`), серверная
валидация PATCH-значения (`settings_validation.validate_setting_value`) и точечная правка
пустого гейта в `settings_ops.validate_batch_item`.

Чистые функции — без БД, без FastAPI, без aiogram (см. `<verification>` плана).
`modcard_fields` — единственная сегодня запись `type: "multi"` в реестре, поэтому кейсы
таблицы `<behavior>` завязаны на неё и на `moderation_card.CARD_STEPS` (43 пары).
"""
from __future__ import annotations

import asyncio

import moderation_card as mc
import settings_ops
from settings_schema import SETTINGS_SCHEMA, _parse_setting, multi_codes, multi_labels, multi_options
from settings_validation import validate_setting_value


def _run(coro):
    return asyncio.run(coro)


# ── чтение (бот не должен заметить разницы) ─────────────────────────────────────────────

def test_parse_setting_reads_multi_exactly_like_list():
    assert _parse_setting("modcard_fields", "age\ncity") == ["age", "city"]
    assert _parse_setting("modcard_fields", None) == list(mc.DEFAULT_CARD_STEPS)
    assert len(_parse_setting("modcard_fields", None)) == 20
    assert _parse_setting("modcard_fields", "—") == ["—"]
    assert mc.enabled_steps(_parse_setting("modcard_fields", "—")) == []


def test_parse_setting_multi_parity_with_list_type_literal():
    # Ожидание зафиксировано литералом (не вторым вызовом реестра) — сравнение с самим
    # собой ничего бы не доказало.
    cases = {
        None: list(mc.DEFAULT_CARD_STEPS),
        "age\ncity": ["age", "city"],
        "age;city": ["age", "city"],
        "—": ["—"],
        "": list(mc.DEFAULT_CARD_STEPS),
    }
    for raw, expected in cases.items():
        assert _parse_setting("modcard_fields", raw) == expected


# ── маппинг код<->подпись ────────────────────────────────────────────────────────────────

def test_multi_options_matches_card_steps_order_and_length():
    options = multi_options("modcard_fields")
    assert options == list(mc.CARD_STEPS.items())
    assert len(options) == 43


def test_multi_options_empty_for_non_multi_or_unknown_key():
    assert multi_options("modcard_answer_limit") == []
    assert multi_options("no-such-key") == []


def test_multi_labels_orders_by_card_steps_and_drops_unknown_codes():
    assert multi_labels("modcard_fields", ["city", "age"]) == ["🎂 Возраст", "🏙 Город"]
    assert multi_labels("modcard_fields", ["city", "нет-такого-шага"]) == ["🏙 Город"]
    assert multi_labels("modcard_fields", []) == []


def test_multi_codes_round_trips_labels_to_codes_in_registry_order():
    codes, bad = multi_codes("modcard_fields", ["🏙 Город", "🎂 Возраст"])
    assert bad is None
    assert codes == ["age", "city"]


def test_multi_codes_reports_first_unknown_label():
    codes, bad = multi_codes("modcard_fields", ["🎂 Возраст", "Чужой вариант"])
    assert codes is None
    assert bad == "Чужой вариант"


# ── валидация PATCH-значения ────────────────────────────────────────────────────────────

def test_validate_setting_value_accepts_labels_any_order_any_separator():
    value, error = validate_setting_value("modcard_fields", "🎂 Возраст;🏙 Город")
    assert error is None
    assert value == "age\ncity"  # порядок CARD_STEPS, разделитель \n — байт-в-байт бот


def test_validate_setting_value_empty_becomes_sentinel_not_default():
    value, error = validate_setting_value("modcard_fields", "")
    assert error is None
    assert value == "—" == mc.EMPTY_SENTINEL


def test_validate_setting_value_rejects_unknown_label_without_leaking_codes():
    value, error = validate_setting_value("modcard_fields", "🎂 Возраст;Чужой вариант")
    assert value is None
    assert error is not None
    for code in mc.CARD_STEPS:
        assert code not in error
    assert "_" not in error


def test_validate_setting_value_rejects_a_code_sent_instead_of_a_label():
    # Клиент прислал код (не подпись) — закрытый набор задан подписями, код не вариант.
    value, error = validate_setting_value("modcard_fields", "age")
    assert value is None
    assert error is not None


def test_validate_setting_value_multi_error_text_is_human_and_actionable():
    _value, error = validate_setting_value("modcard_fields", "Чужой вариант")
    assert "галоч" in error or "вариант" in error


# ── settings_ops: пустой гейт пропускает multi, но не остальные типы ───────────────────

def test_validate_batch_item_empty_multi_becomes_sentinel_no_error():
    kw = dict(visible_codes=[], selected_city=None, cities_on=False)
    result = _run(settings_ops.validate_batch_item("modcard_fields", "", **kw))
    assert result.error is None
    assert result.value == "—"


def test_validate_batch_item_empty_other_type_still_rejected():
    kw = dict(visible_codes=[], selected_city=None, cities_on=False)
    result = _run(settings_ops.validate_batch_item("event_name", "  ", **kw))
    assert result.error == settings_ops.EMPTY_VALUE_TEXT
    assert result.value is None


def test_validate_batch_item_unknown_label_rejected_db_untouched():
    kw = dict(visible_codes=[], selected_city=None, cities_on=False)
    result = _run(settings_ops.validate_batch_item("modcard_fields", "Чужой вариант", **kw))
    assert result.value is None
    assert result.error is not None


# ── сторожа реестра ──────────────────────────────────────────────────────────────────────

def test_registry_multi_entries_are_well_formed():
    multi_entries = {k: v for k, v in SETTINGS_SCHEMA.items() if v.get("type") == "multi"}
    assert multi_entries, "ожидался хотя бы один ключ type=multi (modcard_fields)"
    for key, entry in multi_entries.items():
        labels = [label for _code, label in multi_options(key)]
        assert len(labels) == len(set(labels)), f"{key}: дублирующиеся подписи"
        for label in labels:
            assert ";" not in label and "|" not in label and "\n" not in label, key
        codes = {code for code, _label in multi_options(key)}
        default = entry.get("default") or []
        assert set(default) <= codes, f"{key}: default вне закрытого набора"
        assert entry.get("empty_value") == mc.EMPTY_SENTINEL, key


def test_modcard_fields_registry_meta():
    entry = SETTINGS_SCHEMA["modcard_fields"]
    assert entry["type"] == "multi"
    assert entry["options_ref"] == "moderation_card:CARD_STEPS"
    assert entry["empty_value"] == mc.EMPTY_SENTINEL
