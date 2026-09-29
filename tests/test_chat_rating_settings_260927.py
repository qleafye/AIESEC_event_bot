"""Настройки рейтинга чата в реестре: группа «💬 Чат делегатов», числа — из chat_score.

Второго источника дефолтов нет: реестр строит их из chat_score, дашборд и тул считают по тем
же числам. Формат "number" — дробь через запятую, 0 допустим, отрицательное и мусор — нет.
"""
import re

from core import chat_score as cs
from core.settings_schema import SETTINGS_SCHEMA, _parse_setting
from core.settings_validation import validate_setting_value

FORMULA_KEYS = list(cs.SETTING_KEYS.values()) + [cs.BURST_GAP_KEY, cs.RETENTION_KEY]
PER_CITY_KEYS = (
    [cs.MODE_KEY] + list(cs.RULE_SETTING_KEYS.values())
    + [cs.CURRENCY_KEY, cs.SOCIAL_TASKS_KEY]
)
ALL_KEYS = FORMULA_KEYS + PER_CITY_KEYS


def test_every_chat_key_is_registered_in_group_chat():
    for key in ALL_KEYS:
        assert key in SETTINGS_SCHEMA, key
        assert SETTINGS_SCHEMA[key]["group"] == "chat", key


def test_defaults_come_from_chat_score():
    for name, key in cs.SETTING_KEYS.items():
        assert SETTINGS_SCHEMA[key]["default"] == f"{cs.WEIGHTS[name]:g}", key
    for name, key in cs.RULE_SETTING_KEYS.items():
        entry = SETTINGS_SCHEMA[key]
        if entry["type"] == "int":
            assert entry["default"] == cs.RULES_DEFAULTS[name], key
        else:
            assert entry["default"] == f"{cs.RULES_DEFAULTS[name]:g}", key
    assert SETTINGS_SCHEMA[cs.BURST_GAP_KEY]["default"] == 120
    assert SETTINGS_SCHEMA[cs.RETENTION_KEY]["default"] == 180
    assert SETTINGS_SCHEMA[cs.MODE_KEY]["default"] == "formula"
    assert SETTINGS_SCHEMA[cs.MODE_KEY]["options"] == list(cs.MODES)


def test_post_min_chars_zero_both_unset_and_set():
    key = cs.RULE_SETTING_KEYS["post_min_chars"]
    assert _parse_setting(key, None) == 0
    assert _parse_setting(key, "0") == 0


def test_per_city_flags():
    for key in PER_CITY_KEYS:
        assert SETTINGS_SCHEMA[key].get("per_city") is True, key
    for key in FORMULA_KEYS:
        assert not SETTINGS_SCHEMA[key].get("per_city"), key


def test_number_format_accepts_comma_and_zero():
    assert validate_setting_value("chat_rules_comment_points__city__spb", "12,5") == ("12.5", None)
    assert validate_setting_value("chat_rating_reply_weight", " 0 ") == ("0", None)
    assert validate_setting_value("chat_rating_reply_weight", "3") == ("3", None)


def test_number_format_rejects_garbage_and_negative():
    for bad in ("abc", "-1", "nan", "inf", ""):
        value, error = validate_setting_value("chat_rating_reply_weight", bad)
        assert value is None, bad
        assert "<code>" in error


def test_registry_defaults_parse_back_to_chat_score_defaults():
    raw = {k: SETTINGS_SCHEMA[k]["default"] for k in ALL_KEYS}
    raw = {k: (None if v is None else str(v)) for k, v in raw.items()}
    weights, gap = cs.weights_from_settings(raw)
    assert weights == cs.WEIGHTS
    assert gap == cs.DEFAULT_BURST_GAP
    assert cs.rules_from_settings(raw) == cs.RULES_DEFAULTS


def test_no_codes_in_labels_and_prompts():
    for key in ALL_KEYS:
        entry = SETTINGS_SCHEMA[key]
        for field in ("label", "prompt"):
            text = entry.get(field) or ""
            assert "chat_rating" not in text and "chat_rules" not in text, (key, field)


def test_currency_default_is_cyrillic():
    default = SETTINGS_SCHEMA[cs.CURRENCY_KEY]["default"]
    assert default == cs.DEFAULT_CURRENCY
    assert not re.search(r"[A-Za-z]", default)


def test_mode_has_human_option_labels():
    labels = SETTINGS_SCHEMA[cs.MODE_KEY].get("option_labels")
    assert set(labels) == set(cs.MODES)
    assert all(not re.search(r"[A-Za-z]", v) for v in labels.values())
