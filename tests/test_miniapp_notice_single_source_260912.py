"""Квик 12.09 (UI-аудит, пункт 8) — один источник тоста и текста ошибки вместо девяти копий.

Раньше `errorText`/`say` были дословными дублями в `review.js`, `admin_coins.js`,
`admin_faq.js`, `task_edit.js`, `applications.js`, `questions.js`, `screens/form.js`.
Канон — `ui.js::errorText`/`ui.js::noticeBox`; `form.js` оставляет реэкспорт `errorText`.
`screens/submit.js::say` НЕ переводится — другая вёрстка (`.error-inline`) и роль (инлайн-
ошибка формы, не тост).

Хелперы — из read-only `tests/test_miniapp_frontend.py` (импорт, не правка).
"""
from __future__ import annotations

from tests.test_miniapp_frontend import MINIAPP_STATIC, SCREENS_DIR, _js_without_comments

UI_JS = MINIAPP_STATIC / "js" / "ui.js"
FORM_JS = MINIAPP_STATIC / "js" / "form.js"

NOTICE_SCREENS = [
    "review.js",
    "admin_coins.js",
    "admin_faq.js",
    "task_edit.js",
    "applications.js",
    "questions.js",
    "form.js",
]


def _all_js_files():
    files = [UI_JS, FORM_JS]
    files += sorted(SCREENS_DIR.glob("*.js"))
    return files


def test_error_text_defined_exactly_once_in_ui_js():
    hits = [
        f for f in _all_js_files()
        if "function errorText(" in _js_without_comments(f)
    ]
    assert hits == [UI_JS], f"errorText определён не только в ui.js: {hits}"


def test_form_js_reexports_error_text_from_ui():
    text = _js_without_comments(FORM_JS)
    assert "function errorText(" not in text
    assert "errorText" in text
    assert 'from "./ui.js"' in text


def test_say_function_only_in_submit_js():
    hits = [
        f.name for f in sorted(SCREENS_DIR.glob("*.js"))
        if "function say(" in _js_without_comments(f)
    ]
    assert hits == ["submit.js"], f"function say( осталась вне submit.js: {hits}"


def test_notice_screens_import_notice_box_from_ui():
    for name in NOTICE_SCREENS:
        path = SCREENS_DIR / name
        text = _js_without_comments(path)
        assert "noticeBox" in text, f"{name} не подключает noticeBox"
        assert 'from "../ui.js"' in text, f"{name}: нет импорта из ../ui.js"


def test_notice_box_has_no_cyrillic_literals():
    import re
    text = _js_without_comments(UI_JS)
    fn_start = text.index("export function noticeBox")
    rest = text[fn_start:]
    next_export = rest.index("\nexport function", 1)
    fn_body = rest[:next_export]
    cyrillic = re.findall(r"[А-Яа-яЁё]", fn_body)
    assert not cyrillic, f"noticeBox содержит кириллицу: {fn_body}"
