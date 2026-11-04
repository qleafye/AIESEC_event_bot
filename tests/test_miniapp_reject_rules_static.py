"""Статические сторожа экрана списка правил автоотказа.

Маршруты, плитка, иконка, лист и стили проверяются по тексту файлов — без браузера.
Доменные коды (шаг анкеты, код города) в экран не попадают.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_miniapp_frontend import APP_JS, _js_without_comments
from tests.test_web_assets_260823 import EXPECTED_ICON_NAMES

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "miniapp" / "static" / "js"
APP_CSS = ROOT / "miniapp" / "static" / "app.css"
API_JS = JS / "api.js"
SHEET_JS = JS / "rr_sheet.js"
SCREEN_JS = JS / "screens" / "admin_reject_rules.js"
PHASE_CSS = "/* Phase 37: редактор правил автоотказа */"


def test_routes_nav_and_icon():
    text = APP_JS.read_text(encoding="utf-8")
    routes = text[text.index("export const ROUTES"):text.index("export const SECTION_GROUPS")]
    assert '"#/admin-reject-rules"' in routes
    assert '"#/admin-reject-rule/{id}"' in routes
    assert "screens/admin_reject_rules.js" in routes
    assert "screens/admin_reject_rule.js" in routes
    nav = text[text.index("export const NAV ="):text.index("export const NAV_ICONS")]
    row = nav[nav.index('"#/admin-reject-rules"'):nav.index("},", nav.index('"#/admin-reject-rules"'))]
    assert 'section: "settings"' in row
    assert 'cap: "settings"' in row
    assert 'group: "apps"' in row
    icons = text[text.index("export const NAV_ICONS"):text.index("const compiled")]
    assert '"#/admin-reject-rules": "ban"' in icons
    assert "ban" in EXPECTED_ICON_NAMES
    label = _js_without_comments(APP_JS)
    body = label[label.index("function navLabel"):]
    body = body[:body.index("\n}\n")]
    assert "item.label" in body
    assert "labelText(" in body


def test_sheet_and_screen_have_no_inner_html():
    for path in (SHEET_JS, SCREEN_JS):
        assert "innerHTML" not in path.read_text(encoding="utf-8")
    sheet = SHEET_JS.read_text(encoding="utf-8")
    assert "export function openSheet" in sheet
    assert "window.Telegram?.WebApp?.BackButton" in sheet


def test_phase_css_has_no_hex_or_gradient():
    css = APP_CSS.read_text(encoding="utf-8")
    block = css[css.index(PHASE_CSS):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}", block)
    assert "gradient" not in block.lower()
    assert len(re.findall(r"\.rr-", block)) >= 15


def test_api_accepts_signal():
    assert "signal" in API_JS.read_text(encoding="utf-8")


def test_list_screen_uses_shared_pieces_and_no_codes():
    text = SCREEN_JS.read_text(encoding="utf-8")
    assert "export async function render" in text
    assert "guardedRender" in text
    assert "openSheet" in text
    assert '"/admin/reject-rules/master"' in text
    assert "confirm" in text
    assert "can_toggle_master" in text
    assert "can_edit" in text
    assert "innerHTML" not in text
    assert "age_on_forum_lt" not in text
    assert "msk" not in text
