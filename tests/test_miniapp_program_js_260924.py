"""D-29 — фронт экрана «📅 Программа» Mini App (`miniapp/static/js/screens/program.js`):
статические сторожа без DOM (в проекте нет запускалки JS-тестов), тот же приём, что
`tests/test_miniapp_frontend.py`. Хелперы — оттуда же (импорт, не правка)."""
from __future__ import annotations

import re

from tests.test_miniapp_frontend import (
    APP_JS,
    MINIAPP_STATIC,
    SCREENS_DIR,
    _HEX_OR_RGB_COLOR,
    _js_without_comments,
)

PROGRAM_JS = SCREENS_DIR / "program.js"
APP_CSS = MINIAPP_STATIC / "app.css"

_CYRILLIC_LITERAL = re.compile(r"""(["'`])[^"'`\n]*[А-Яа-яЁё][^"'`\n]*\1""")


def _program_text() -> str:
    return _js_without_comments(PROGRAM_JS)


def test_program_screen_exports_render_via_guarded_render():
    text = _program_text()
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text)
    assert "guardedRender(" in text


def test_program_screen_is_safe_and_self_contained():
    text = _program_text()
    assert "innerHTML" not in text
    assert "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text)
    assert "https://" not in text and "http://" not in text
    # Без внешних библиотек: импорты только из своих модулей Mini App.
    for spec in re.findall(r'from\s+"([^"]+)"', text):
        assert spec.startswith("../"), spec


def test_program_screen_has_no_russian_literals():
    """Все подписи — с сервера (`texts`, `empty_text`, подпись раздела): EN приходит
    переведённым, в JS нечего переводить."""
    assert not _CYRILLIC_LITERAL.findall(_program_text())


def test_program_screen_uses_server_flags_and_texts():
    text = _program_text()
    assert 'api("/program")' in text
    assert "slot.now" in text and "slot.next" in text
    assert "texts.now" in text and "texts.next" in text
    assert "texts.hall" in text and "texts.speaker" in text


def test_program_screen_covers_three_views():
    text = _program_text()
    assert 'page.view === "photo"' in text and "page.photo_url" in text
    assert 'page.view === "table"' in text
    assert "emptyState(" in text and "page.empty_text" in text


def test_program_screen_groups_parallel_sessions():
    text = _program_text()
    assert "slot.sessions.length > 1" in text
    assert "program-parallel" in text


def test_program_photo_opens_large_and_closes_on_navigation():
    text = _program_text()
    assert "openZoom(" in text
    assert '"hashchange"' in text
    assert '"Escape"' in text


def test_program_screen_handles_retry_503_with_server_text():
    text = _program_text()
    assert 'err.reason === "retry"' in text
    assert "errorText(err" in text


def test_program_title_from_section_label_via_labeltext():
    assert 'labelText(sectionLabel("program"))' in _program_text()


def test_program_css_classes_exist_and_are_tokenised():
    css = APP_CSS.read_text(encoding="utf-8")
    block = css[css.index(".program-days"):]
    for cls in re.findall(r'class:\s*"([^"]+)"', _program_text()):
        for name in cls.split():
            if name.startswith("program-"):
                assert f".{name}" in css, name
    assert not _HEX_OR_RGB_COLOR.findall(block)


def test_program_nav_item_and_icon_in_app_js():
    text = APP_JS.read_text(encoding="utf-8")
    assert '{ hash: "#/program", section: "program", delegate: true }' in text
    assert '"#/program": "calendar"' in text
    assert '["#/program", "screens/program.js"]' in text
