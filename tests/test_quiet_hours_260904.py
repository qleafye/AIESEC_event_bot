"""Quick 260904-dq1: «🌙 Тихие часы» для уведомлений делегатам.

Файл наполняется по задачам плана:
- Task 1 (этот срез) — реестровая часть: четыре ключа существуют, дефолты/типы/группы
  верные, валидатор `format: "time"` принимает/отклоняет по правилам.
- Task 2 — окно (`services.quiet_hours.is_quiet`/`next_window_end`/`window_for_city`),
  очередь `delayed_notifications`, джоба `flush_due`.
- Task 3 — обёртка `apply_decision_effects`, дедуп «последнее решение», приписка менеджеру.
- Task 4 — гейма/монеты/напоминания.
- Task 5 — предупреждение о рассылке, счётчик очереди.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, `config.DB_PATH`
указывает на файл в `tmp_path` (конвенция сьюта, см. `tests/test_settings_int_validation_260819.py`).
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from settings_validation import validate_setting_value


# ── Task 1: реестр ──────────────────────────────────────────────────────────────────────

def test_quiet_hours_enabled_is_toggles_enum_default_off():
    entry = SETTINGS_SCHEMA["quiet_hours_enabled"]
    assert entry["type"] == "enum"
    assert entry["group"] == "toggles"
    assert entry["options"] == ["on", "off"]
    assert entry["prompt"] is None
    assert entry["default"] == "off"
    assert not entry.get("per_city")


@pytest.mark.parametrize("key", ["quiet_hours_start", "quiet_hours_end"])
def test_quiet_hours_time_keys_are_per_city_text_with_time_format(key):
    entry = SETTINGS_SCHEMA[key]
    assert entry["type"] == "text"
    assert entry["group"] == "apps"
    assert entry.get("per_city") is True
    assert entry.get("format") == "time"


def test_quiet_hours_start_end_defaults():
    assert SETTINGS_SCHEMA["quiet_hours_start"]["default"] == "22:00"
    assert SETTINGS_SCHEMA["quiet_hours_end"]["default"] == "09:00"


def test_quiet_hours_manager_notice_text_is_global_text():
    entry = SETTINGS_SCHEMA["quiet_hours_manager_notice_text"]
    assert entry["type"] == "text"
    assert entry["group"] == "apps"
    assert not entry.get("per_city")
    assert "{time}" in entry["default"]


@pytest.mark.parametrize("raw, expected", [
    ("22:00", "22:00"),
    ("9:00", "09:00"),
    ("00:00", "00:00"),
    (" 09:05 ", "09:05"),
])
def test_time_format_validator_accepts_and_normalizes(raw, expected):
    value, error = validate_setting_value("quiet_hours_start", raw)
    assert error is None
    assert value == expected


@pytest.mark.parametrize("raw", ["25:00", "22:60", "22-00", "вечером", "", "9", "99:99"])
def test_time_format_validator_rejects_with_example(raw):
    value, error = validate_setting_value("quiet_hours_end", raw)
    assert value is None
    assert error
    assert "22:00" in error
    assert "«-»" in error


def test_time_format_validator_per_city_composite_uses_base_type():
    value, error = validate_setting_value("quiet_hours_start__city__msk", "25:99")
    assert value is None and error
    value, error = validate_setting_value("quiet_hours_start__city__msk", "9:00")
    assert value == "09:00" and error is None


def _ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
