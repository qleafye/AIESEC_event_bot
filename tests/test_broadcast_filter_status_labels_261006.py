"""Фильтр рассылки по статусу заявки показывает слова, а не pending/approved/rejected."""
from handlers import admin_broadcasts as ab


def test_value_picker_shows_human_status_labels():
    kb = ab._value_picker_kb("status", ["pending", "approved", "rejected"], 0)
    texts = [row[0].text for row in kb.inline_keyboard[:3]]
    assert texts == ["Новая", "Одобрена", "Отклонена"]


def test_filter_summary_shows_human_status_label():
    summary = ab._filter_summary([{"field": "status", "value": "approved"}])
    assert "Одобрена" in summary and "approved" not in summary


def test_payment_status_labels_unchanged():
    kb = ab._value_picker_kb("payment_status", ["paid"], 0)
    assert kb.inline_keyboard[0][0].text == "Оплатил"
