"""Сторож конструктора условий: контракт модуля и человеческие строки, без браузера."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "miniapp" / "static" / "js" / "rr_conditions.js"


def _text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_exports_and_no_inner_html():
    text = _text()
    assert "export function renderConditions" in text
    assert "export function validateGroups" in text
    assert "innerHTML" not in text
    assert "openSheet" in text
    assert not re.search(r"#[0-9a-fA-F]{3,8}", text)


def test_question_sheet_and_block_copy():
    text = _text()
    assert "ИЛИ" in text
    assert "и ещё условие" in text
    assert "Показаны только включённые вопросы" in text


def test_value_controls():
    text = _text()
    assert "inputmode" in text
    assert 'type: "date"' in text
    assert "Нужно целое число, например 18." in text
    assert "не указана дата форума" in text
    assert "radiogroup" in text
    assert "focusFirstError" in text
