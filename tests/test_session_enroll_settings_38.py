"""Реестр настроек записи на сессии и теста компетенций: ключи, дедлайн, подстановки."""
import re

import settings_ops
from settings_schema import SETTINGS_SCHEMA
from settings_validation import validate_setting_value

_PH = re.compile(r"\{(\w+)\}")
_PHASE_KEYS = [
    k for k in SETTINGS_SCHEMA
    if k.startswith("session_enroll_") or k.startswith("quiz_")
]


def test_all_phase_keys_registered():
    assert len(_PHASE_KEYS) >= 45
    for key in _PHASE_KEYS:
        meta = SETTINGS_SCHEMA[key]
        assert meta["group"] == "event", key
        if meta["type"] == "text":
            assert meta["per_city"] is True, key
            assert meta["prompt"], key


def test_enroll_toggle_off_by_default():
    meta = SETTINGS_SCHEMA["session_enroll_enabled"]
    assert meta["default"] == "off"
    assert meta["options"] == ["on", "off"]
    assert meta["per_city"] is True


def test_deadline_datetime_validation():
    key = "session_enroll_deadline"
    assert validate_setting_value(key, "28.10.2026 23:59") == ("28.10.2026 23:59", None)
    assert validate_setting_value(key, " 1.9.2026 9:05 ") == ("01.09.2026 09:05", None)
    for bad in ("28.10.2026", "28.10", "завтра"):
        value, err = validate_setting_value(key, bad)
        assert value is None and "28.10.2026 23:59" in err


def test_per_city_deadline_key_validated():
    key = "session_enroll_deadline__city__msk"
    assert validate_setting_value(key, "28.10.2026 23:59")[1] is None
    assert validate_setting_value(key, "завтра")[0] is None


def test_placeholders_declared():
    for key in _PHASE_KEYS:
        meta = SETTINGS_SCHEMA[key]
        if meta["type"] != "text":
            continue
        names = set(_PH.findall(meta["default"]))
        if not names:
            continue
        assert names <= set(meta.get("placeholders", {})), key
        assert "скобки бот заменит сам" in meta["prompt"].lower(), key


def test_preview_samples_cover_new_placeholders():
    for name in ("day", "old", "new", "competency", "level"):
        assert name in settings_ops.PREVIEW_SAMPLES


def test_not_in_event_field_order():
    from handlers.admin_settings import _EVENT_FIELD_ORDER

    assert not [k for k in _PHASE_KEYS if k in _EVENT_FIELD_ORDER]
