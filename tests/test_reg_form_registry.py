"""Phase 21 Plan 02 (FORM-SYNC-04, D-25): реестр текстов анкеты Mini App — сторож полноты и
человечности для планов 21-07..21-11, которые эти ключи ЧИТАЮТ, а не заводят заново.

Закрытый список из 30 текстовых ключей группы «reg» — добавление 31-го обязано осознанно
ломать этот тест. Группа большинства ключей — "reg" (D-25), не "miniapp" (имена
`reg_form_*`/`reg_resume_*` не совпадают с UI-SPEC `miniapp_form_*` намеренно — префикс
`miniapp_` у КАЖДОГО ключа обязан иметь group == "miniapp", tests/test_miniapp_registry.py).

pytest-asyncio недоступен в этом окружении — тест синхронный, чтения только из
SETTINGS_SCHEMA (модуль-уровня dict, БД не требуется).
"""
import re

from settings_schema import SETTINGS_SCHEMA

# Закрытый список 30 текстов фазы (Task 1) — добавление 31-го обязано осознанно ломать
# этот тест.
REG_FORM_TEXT_KEYS = [
    "reg_form_cta_text",
    "reg_resume_continue_label",
    "reg_resume_restart_label",
    "reg_resume_restart_confirm_text",
    "reg_sync_from_app_text",
    "reg_form_conflict_text",
    "reg_form_not_set_text",
    "reg_form_submit_cta_text",
    "reg_form_edit_submit_cta_text",
    "reg_form_cancel_changes_text",
    "reg_form_cancel_changes_confirm_text",
    "reg_form_continue_in_chat_text",
    "reg_form_consent_required_text",
    "reg_form_resume_too_large_text",
    "reg_form_resume_wrong_type_text",
    "reg_form_prior_answer_badge_text",
    "reg_form_updated_in_chat_badge_text",
    "reg_form_complete_heading_text",
    "reg_form_complete_body_text",
    "reg_form_edited_heading_text",
    "reg_form_edited_pending_heading_text",
    "reg_form_resubmit_heading_text",
    "reg_form_closed_text",
    "reg_form_rejected_banner_text",
    "reg_form_profile_edit_cta_text",
    "reg_edited_admin_label",
    "reg_resubmit_admin_label",
    "reg_edit_history_button_label",
    "reg_nudge_chat_button_text",
    "reg_nudge_app_button_text",
]

# Закрытый список per_city (ровно 9 — тексты, обращённые к делегату, per UI-SPEC §
# Copywriting Contract, колонка per_city «да»).
PER_CITY_KEYS = {
    "reg_form_cta_text",
    "reg_nudge_app_button_text",
    "reg_form_complete_heading_text",
    "reg_form_complete_body_text",
    "reg_form_edited_heading_text",
    "reg_form_edited_pending_heading_text",
    "reg_form_resubmit_heading_text",
    "reg_form_closed_text",
    "reg_form_rejected_banner_text",
}

_PLACEHOLDER_RE = re.compile(r"\{\w+\}")


def test_exactly_30_reg_form_keys():
    assert len(REG_FORM_TEXT_KEYS) == 30
    assert len(set(REG_FORM_TEXT_KEYS)) == 30


def test_every_key_in_registry_as_reg_text():
    for key in REG_FORM_TEXT_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "reg", key
        assert entry["type"] == "text", key


def test_every_key_has_human_default_and_no_code_in_label():
    for key in REG_FORM_TEXT_KEYS:
        entry = SETTINGS_SCHEMA[key]
        default = entry["default"]
        assert isinstance(default, str) and default.strip(), key
        label = entry["label"]
        assert isinstance(label, str) and label.strip(), key
        # Код ключа человеку не показываем (CLAUDE.md «бот для людей»).
        assert "reg_form" not in label, key
        assert "reg_resume" not in label, key


def test_per_city_set_matches_closed_list():
    actual = {
        k for k in REG_FORM_TEXT_KEYS
        if SETTINGS_SCHEMA[k].get("per_city")
    }
    assert actual == PER_CITY_KEYS, (
        f"расхождение: только фактически {sorted(actual - PER_CITY_KEYS)}, "
        f"только в списке плана {sorted(PER_CITY_KEYS - actual)}"
    )
    # Ключи вне закрытого списка per_city обязаны не иметь флага вовсе.
    for key in REG_FORM_TEXT_KEYS:
        if key not in PER_CITY_KEYS:
            assert not SETTINGS_SCHEMA[key].get("per_city"), key


def test_default_placeholders_are_described_in_prompt():
    for key in REG_FORM_TEXT_KEYS:
        entry = SETTINGS_SCHEMA[key]
        default = entry["default"]
        prompt = entry.get("prompt") or ""
        for placeholder in _PLACEHOLDER_RE.findall(default):
            assert placeholder in prompt, (
                f"{key}: дефолт содержит {placeholder!r}, но prompt его не описывает"
            )
