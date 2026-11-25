"""Игла screens/settings.js: клик чипа вставляет {token}, на кнопке — человеческая подпись.

Без node, тот же приём, что tests/test_miniapp_settings_notice_js_261010.py.
Глаз-превью (hasPlaceholder / icon eye) этот ряд не заменяет.
"""
from __future__ import annotations

import re

from tests._paths import REPO_ROOT

SETTINGS_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "settings.js"


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_chip_click_inserts_braced_token_and_button_shows_label():
    text = SETTINGS_JS.read_text(encoding="utf-8")
    insert = _between(text, "function insertPlaceholderToken(", "function mountPlaceholderChips(")
    mount = _between(text, "function mountPlaceholderChips(", "function buildRow(")
    row = _between(text, "function buildRow(", "function toggleOverrideList(")

    assert "`{${token}}`" in insert
    assert "typeof control.selectionStart === \"number\"" in insert
    assert "control.value.length" in insert
    assert 'control.dispatchEvent(new Event("input", { bubbles: true }))' in insert

    assert "if (item.editable === false) return;" in mount
    assert "chip.token && chip.label" in mount
    assert re.search(
        r"onClick:\s*\(\)\s*=>\s*insertPlaceholderToken\(control,\s*chip\.token\),\s*"
        r"\}, chip\.label\)\);",
        mount,
    )

    assert "mountPlaceholderChips(el, item);" in row
    assert "field(h, spec, item.value, (v) => onFieldChange(rawItem, el, v));" in row
    assert 'icon("eye")' in text
    # Глаз и у полей с чипами, и у писем делегату (delegate_preview). Чипы от этого не пропадают.
    assert "if ((hasPlaceholder(item) || item.delegate_preview) && !locked)" in text
