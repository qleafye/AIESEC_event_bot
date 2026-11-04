"""Городские флаги: оплата, согласия, модерация, списки и тексты анкеты, гейма, амбассадоры.

Реестр — источник правды (`cities.is_per_city`). Пустое городское переопределение
по-прежнему значит «как везде».
"""
import domain.cities as cities
from domain.settings.schema import SETTINGS_SCHEMA

# Оплата: вся группа pay и два тумблера.
PAY_KEYS = (
    "payment_options",
    "payment_requisites",
    "payment_requisites_by_lc",
    "payment_deadline",
    "payment_reminder_text",
    "payment_overdue_text",
    "penalty_schedule",
    "payment_option_picker_header_text",
    "payment_details_template_text",
    "payment_pay_later_text",
    "payment_pay_later_menu_hint_text",
    "payment_receipt_received_text",
    "payment_enabled",
    "payment_reminders_enabled",
)

# Согласия: вся группа consent и два тумблера.
CONSENT_KEYS = (
    "consent_button_text",
    "consent_list",
    "consent_version",
    "consent_recollect_text",
    "consent_enabled",
    "consent_recollect_enabled",
)

MODERATION_KEYS = (
    "full_approval",
    "short_approval",
    "party_approval",
    "pending_notify_mode",
)

ANKETA_LIST_KEYS = (
    "source_options",
    "city_options",
    "study_field_options",
    "goal_options",
    "formats_options",
    "university_options",
    "stack_options",
    "experience_options",
    "readiness_options",
    "education_status_options",
    "edu_studying_statuses",
)

ANKETA_TEXT_KEYS = (
    "reject_text",
    "reject_rules_return_text",
    "pending_gate_text",
    "nudge_text",
    "nudge_after_minutes",
    "nudge_enabled",
)

NAMED_PER_CITY_KEYS = (
    PAY_KEYS + CONSENT_KEYS + MODERATION_KEYS + ANKETA_LIST_KEYS + ANKETA_TEXT_KEYS
)

STILL_GLOBAL_KEYS = (
    "event_name",
    "event_season",
    "quiet_hours_enabled",
    "approve_text__party",
)


def test_named_handoff_keys_are_per_city():
    missing = [key for key in NAMED_PER_CITY_KEYS if key not in SETTINGS_SCHEMA]
    assert not missing, missing
    for key in NAMED_PER_CITY_KEYS:
        assert cities.is_per_city(key) is True, key


def test_shared_event_keys_stay_global():
    for key in STILL_GLOBAL_KEYS:
        assert key in SETTINGS_SCHEMA, key
        assert cities.is_per_city(key) is False, key


def test_game_and_amb_groups_are_entirely_per_city():
    seen = {"game": 0, "amb": 0}
    for key, meta in SETTINGS_SCHEMA.items():
        group = meta.get("group")
        if group not in seen:
            continue
        seen[group] += 1
        assert cities.is_per_city(key) is True, key
    assert seen["game"] > 0 and seen["amb"] > 0
