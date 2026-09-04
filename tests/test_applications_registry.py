"""Phase 23 Plan 01 Task 3 (APP-TINDER-01, D-05/D-09): реестр раздела «🗂 Отбор заявок» —
сторож в обе стороны (SECTIONS <-> miniapp_section_*), человеческие дефолты 14 новых ключей,
попадание reject_reason_templates в общий списочный редактор бота.

Комплементарен `tests/test_miniapp_registry.py` (число ключей группы `miniapp` целиком) —
этот файл проверяет только новую поверхность фазы 23.
"""
from __future__ import annotations

from handlers.admin_settings import _APPS_FIELD_ORDER
from miniapp.deps import SECTIONS
from settings_schema import SETTINGS_SCHEMA

APPLICATIONS_TEXT_KEYS = [
    "miniapp_empty_applications",
    "miniapp_empty_applications_skipped",
    "miniapp_empty_applications_filtered",
    "miniapp_applications_show_all",
    "miniapp_applications_undo_button",
    "miniapp_applications_approved_toast",
    "miniapp_applications_rejected_toast",
    "miniapp_applications_undone_toast",
    "miniapp_applications_approve_all_confirm",
    "miniapp_applications_reject_no_reason",
    "miniapp_applications_reject_own_reason",
    "miniapp_applications_filter_all",
    "miniapp_applications_filter_changed",
]

NEW_KEYS = APPLICATIONS_TEXT_KEYS + ["miniapp_section_applications"]

# Квик 260904-7e7 (D18): шторка отказа стала модальным листом — своя кнопка отмены. Отдельный
# список (не в NEW_KEYS выше — тот фиксирует ровно 14 ключей плана 23-01, свой контракт).
QUICK_260904_7E7_KEYS = ["miniapp_applications_reject_cancel"]

# Квик 260904-kk6 (D17): та же кнопка «Показать всё» в раскрытом состоянии подписывается
# «Свернуть» — отдельный список тем же приёмом, что QUICK_260904_7E7_KEYS выше.
QUICK_260904_KK6_KEYS = ["miniapp_applications_hide_all"]


# ── Раздел объявлен в обе стороны ────────────────────────────────────────────────────────

def test_every_section_has_a_registry_toggle():
    for section in SECTIONS:
        key = f"miniapp_section_{section}"
        assert key in SETTINGS_SCHEMA, f"{section} без тумблера в реестре"


def test_every_section_toggle_key_has_a_section():
    registry_sections = {
        k[len("miniapp_section_"):] for k in SETTINGS_SCHEMA if k.startswith("miniapp_section_")
    }
    assert registry_sections == set(SECTIONS)


def test_applications_section_declared():
    assert "applications" in SECTIONS
    entry = SETTINGS_SCHEMA["miniapp_section_applications"]
    assert entry["type"] == "enum"
    assert entry["options"] == ["on", "off"]
    assert entry["default"] == "on"


# ── 14 новых ключей: группа miniapp, человеческий default, label без кодов ────────────────

def test_new_keys_are_group_miniapp_with_human_defaults():
    assert len(NEW_KEYS) == 14
    for key in NEW_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "miniapp", key
        assert isinstance(entry["label"], str) and entry["label"].strip(), key
        assert "_" not in entry["label"], key
        assert "miniapp" not in entry["label"], key
        default = entry["default"]
        if entry["type"] == "text":
            assert isinstance(default, str) and default.strip(), key
        else:
            assert default == "on", key


# ── reject_reason_templates правится общим списочным редактором бота ─────────────────────

def test_reject_reason_templates_in_apps_field_order():
    assert "reject_reason_templates" in _APPS_FIELD_ORDER


def test_reject_reason_templates_default_is_human_list():
    entry = SETTINGS_SCHEMA["reject_reason_templates"]
    assert entry["type"] == "list"
    assert entry["group"] == "apps"
    default = entry["default"]
    assert isinstance(default, list) and len(default) >= 3
    for item in default:
        assert isinstance(item, str) and item.strip()
        assert "_" not in item  # человеческая формулировка, не код


# ── Подстановки {count} ───────────────────────────────────────────────────────────────────

def test_count_placeholder_present_in_skipped_and_approve_all():
    assert "{count}" in SETTINGS_SCHEMA["miniapp_empty_applications_skipped"]["default"]
    assert "{count}" in SETTINGS_SCHEMA["miniapp_applications_approve_all_confirm"]["default"]


# ── Квик 260904-7e7 (D18): шторка отказа — модальный лист, своя кнопка отмены ─────────────

def test_reject_cancel_key_registered_with_human_default():
    for key in QUICK_260904_7E7_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text" and entry["group"] == "miniapp", key
        assert isinstance(entry["label"], str) and entry["label"].strip(), key
        assert "_" not in entry["label"] and "miniapp" not in entry["label"], key
        assert isinstance(entry["default"], str) and entry["default"].strip(), key


# ── Квик 260904-kk6 (D17): «Показать всё» / «Свернуть» — обе подписи одной кнопки ────────

def test_hide_all_key_registered_with_human_default():
    for key in QUICK_260904_KK6_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text" and entry["group"] == "miniapp", key
        assert isinstance(entry["label"], str) and entry["label"].strip(), key
        assert "_" not in entry["label"] and "miniapp" not in entry["label"], key
        assert isinstance(entry["default"], str) and entry["default"].strip(), key
