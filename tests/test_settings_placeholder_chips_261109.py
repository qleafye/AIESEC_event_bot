"""Чипы подстановок в item_spec: token и подпись из expected_placeholders.

Настоящий ключ бэклога «название задания / баллы / дедлайн» — напоминание о дедлайне
задания. В реестре у него token `coins` (подпись «число коинов»), не `points` («баллы»):
ключа с набором task+points+deadline нет, новый не заводим.
"""
from __future__ import annotations

import domain.settings.ops as settings_ops
import domain.settings.placeholders as settings_placeholders
from domain.settings.schema import SETTINGS_SCHEMA

REMINDER = "wave_deadline_reminder_text"


def _chips(key: str) -> list[dict[str, str]]:
    return settings_ops.item_spec(key, raw=None, value="", is_default=True)["placeholders"]


def test_deadline_reminder_chips_use_human_labels_and_tokens():
    expected = [
        {"token": token, "label": label}
        for token, label in settings_placeholders.expected_placeholders(REMINDER).items()
        if label
    ]
    chips = _chips(REMINDER)
    assert chips == expected
    by_token = {chip["token"]: chip["label"] for chip in chips}
    assert by_token["task"] == "название задания"
    assert by_token["deadline"] == "дата закрытия записи"
    assert by_token["coins"] == "число коинов"
    assert "points" not in by_token
    assert all(chip["label"] and "{" not in chip["label"] for chip in chips)


def test_when_and_threshold_chips_use_human_labels():
    """Пустая подпись чип прячет. Дата и порог должны вставляться токеном, не образцом."""
    when = {chip["token"]: chip["label"] for chip in _chips("interview_reminder_text")}
    assert when["when"] == "дата и время"
    assert "{when}" not in when["when"]
    threshold = {
        chip["token"]: chip["label"] for chip in _chips("amb_invite_threshold_left_text")
    }
    assert threshold["threshold"] == "порог приглашённых"
    assert "5" not in threshold["threshold"]


def test_city_composite_reuses_base_placeholder_chips():
    assert _chips(f"{REMINDER}__city__msk") == _chips(REMINDER)


def test_toggle_and_non_text_have_no_placeholder_chips():
    keys = {
        "toggle": "reg_q_age",
        "enum": "miniapp_enabled",
        "photo": "program",
        "file": "reg_bonus",
        "text": "event_name",
    }
    for kind, key in keys.items():
        assert SETTINGS_SCHEMA[key]["type"] == kind, key
        assert _chips(key) == [], key
