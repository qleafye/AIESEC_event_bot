"""Квик 12.09 (UI-аудит, пункты 1, 7, 11) — структурные сторожа без DOM.

- Пункт 1: верхние табы (`renderToptabs`) несут ту же Lucide-иконку, что таб-бар
  (`renderTabbar`), и подпись без дублирующего ведущего эмодзи (`navLabel` через `labelText`).
- Пункт 7: кнопки создания на менеджерских экранах несут иконку `plus`, не `check`.
- Пункт 11: плита хаба несёт скелетон-плейсхолдер и снимает его после `Promise.allSettled`.

Хелперы — из read-only `tests/test_miniapp_frontend.py` (импорт, не правка).
"""
from __future__ import annotations

from tests.test_miniapp_frontend import APP_JS, ICONS_JS, SCREENS_DIR, _js_without_comments

ADMIN_TASKS_JS = SCREENS_DIR / "admin_tasks.js"
HUB_JS = SCREENS_DIR / "hub.js"


# ── пункт 1: иконки верхних табов ────────────────────────────────────────────────────────

def test_render_toptabs_uses_nav_icons_like_tabbar():
    text = _js_without_comments(APP_JS)
    toptabs_fn = text[text.index("function renderToptabs"):]
    toptabs_body = toptabs_fn[: toptabs_fn.index("\n}\n")]
    assert "NAV_ICONS[item.hash]" in toptabs_body
    assert "icon(NAV_ICONS[item.hash]" in toptabs_body


def test_nav_label_strips_leading_emoji_via_label_text():
    text = _js_without_comments(APP_JS)
    assert "labelText" in text
    nav_label_fn = text[text.index("function navLabel"):]
    nav_label_body = nav_label_fn[: nav_label_fn.index("\n}\n")]
    assert "labelText(" in nav_label_body


def test_app_js_imports_label_text_from_ui():
    text = APP_JS.read_text(encoding="utf-8")
    assert "labelText" in text
    assert 'from "./ui.js"' in text


# ── пункт 7: иконка создания ─────────────────────────────────────────────────────────────

def test_admin_tasks_create_button_uses_plus_not_check():
    text = _js_without_comments(ADMIN_TASKS_JS)
    assert 'icon("check")' not in text
    assert 'icon("plus")' in text


def test_icons_js_has_plus_geometry():
    text = ICONS_JS.read_text(encoding="utf-8")
    assert '"plus":' in text


# ── пункт 11: скелетон плиты хаба ────────────────────────────────────────────────────────

def test_hub_js_shows_and_clears_skeleton_after_settled():
    text = _js_without_comments(HUB_JS)
    assert "skeleton" in text
    settled_idx = text.index("Promise.allSettled")
    tail = text[settled_idx:]
    # снятие скелетона идёт первой значимой правкой после await, до разбора результатов
    assert "classList.remove(\"skeleton\"" in tail
    remove_idx = tail.index("classList.remove(\"skeleton\"")
    fulfilled_idx = tail.index('status === "fulfilled"')
    assert remove_idx < fulfilled_idx
