"""Плита статуса заявки остаётся в цвете бренда. Состояние — бейдж, не заливка блока."""
from __future__ import annotations

from tests._paths import REPO_ROOT

STATUS_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "status.js"
APP_CSS = REPO_ROOT / "miniapp" / "static" / "app.css"


def test_status_plate_does_not_take_state_color():
    js = STATUS_JS.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")
    assert 'class: "plate plate--status"' in js
    assert "status-${tone}" not in js
    assert ".status-success" not in css
    assert ".status-danger" not in css
    assert "chip ${tone}" in js
