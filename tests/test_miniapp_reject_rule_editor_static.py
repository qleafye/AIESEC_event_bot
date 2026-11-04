"""Сторож экрана редактора правила: контракт строк и ручек, без браузера."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "miniapp" / "static" / "js" / "screens" / "admin_reject_rule.js"


def _text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_exports_and_no_markup_tricks():
    text = _text()
    assert "export async function render" in text
    assert "innerHTML" not in text
    assert "age_on_forum_lt" not in text
    assert "msk" not in text
    assert not re.search(r"#[0-9a-fA-F]{3,8}", text)
    assert len(text.splitlines()) >= 400


def test_draft_save_and_conflict():
    text = _text()
    assert "rr_draft:" in text
    assert "sessionStorage" in text
    assert "base_rev" in text
    assert "Загрузить новую версию" in text
    assert "Сохранить мою поверх" in text
    assert "diffView" in text
    assert "renderConditions" in text
    assert "MainButton" in text


def test_counter_and_enable():
    text = _text()
    assert "/preview" in text
    assert "AbortController" in text
    assert "600" in text
    assert "Включить правило?" in text
    assert "needs_confirm" in text
    assert "/enable" in text
    assert "/disable" in text


def test_copy_and_delete():
    text = _text()
    assert "/copy" in text
    assert "Копия будет выключена" in text
    assert "Выключить вместо удаления" in text
    assert "Удалить навсегда" in text
    assert "confirm=1" in text
