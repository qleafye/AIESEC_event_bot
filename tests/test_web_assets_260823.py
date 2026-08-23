"""Phase 19.1 Plan 01 (D-01, D-06, D-07, D-13, D-14, D-16, D-17, D-21): сторожа новых типов
веб-ассетов дизайн-прохода — шрифты бренда с кириллицей (D-14) и иконки Lucide через
`icons.js` (D-13). Существующие сторожа (`tests/test_miniapp_frontend.py`,
`tests/test_dashboard_render.py`) писались до появления в проекте шрифтов и SVG-иконок и не
покрывают эти два типа ассетов — этот файл закрывает разрыв.

- пары `miniapp/static/fonts/X` / `dashboard/static/fonts/X` существуют и побайтно равны
  (тот же приём, что у tokens.css — дрейф ассетов между двумя образами);
- каждый woff2 содержит кириллицу (весь диапазон U+0410–U+044F);
- `@font-face` в обоих `app.css` ссылаются только на локальные пути;
- `miniapp/static/js/icons.js`: 26 иконок, только `none`/`currentColor` в fill/stroke, без
  `innerHTML` и без `http`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MINIAPP_FONTS = ROOT / "miniapp" / "static" / "fonts"
DASHBOARD_FONTS = ROOT / "dashboard" / "static" / "fonts"
MINIAPP_APP_CSS = ROOT / "miniapp" / "static" / "app.css"
DASHBOARD_APP_CSS = ROOT / "dashboard" / "static" / "app.css"
ICONS_JS = ROOT / "miniapp" / "static" / "js" / "icons.js"

FONT_FILES = ("lato-400.woff2", "lato-700.woff2", "raleway-800.woff2", "raleway-800italic.woff2")

EXPECTED_ICON_NAMES = {
    "target", "coins", "trophy", "user", "check-circle-2", "clipboard-list", "wallet",
    "bar-chart-2", "settings", "more-horizontal", "chevron-right", "chevron-down", "clock",
    "x", "trash-2", "archive", "rotate-ccw", "image", "file-text", "link", "pen-line",
    "alert-triangle", "check", "search", "sparkles", "coin",
}


# ── шрифты: пары существуют и побайтно равны между miniapp/dashboard ────────────────────────

@pytest.mark.parametrize("filename", FONT_FILES)
def test_font_file_exists_in_both_static_dirs(filename):
    miniapp_path = MINIAPP_FONTS / filename
    dashboard_path = DASHBOARD_FONTS / filename
    assert miniapp_path.is_file(), f"нет {miniapp_path.relative_to(ROOT)}"
    assert dashboard_path.is_file(), f"нет {dashboard_path.relative_to(ROOT)}"


@pytest.mark.parametrize("filename", FONT_FILES)
def test_font_file_is_byte_for_byte_copy_between_dashboard_and_miniapp(filename):
    miniapp_bytes = (MINIAPP_FONTS / filename).read_bytes()
    dashboard_bytes = (DASHBOARD_FONTS / filename).read_bytes()
    assert miniapp_bytes == dashboard_bytes, (
        f"{filename} разошёлся между miniapp/static/fonts и dashboard/static/fonts — скопируйте заново"
    )


# ── шрифты: кириллица присутствует (D-14) ────────────────────────────────────────────────

@pytest.mark.parametrize("filename", FONT_FILES)
def test_font_file_covers_cyrillic_a_to_ya(filename):
    ttLib = pytest.importorskip("fontTools.ttLib", reason="pip install -r requirements-dev.txt")
    font = ttLib.TTFont(str(MINIAPP_FONTS / filename))
    cmap = font.getBestCmap()
    missing = [hex(code) for code in range(0x0410, 0x0450) if code not in cmap]
    assert not missing, f"{filename}: нет глифов кириллицы {missing}"


# ── @font-face: только локальные пути, никаких внешних хостов ───────────────────────────────

_FONT_FACE_URL = re.compile(r"@font-face\s*\{[^}]*?url\(([^)]+)\)", re.S)


def _font_face_urls(css_text: str) -> list[str]:
    urls = []
    for block_match in re.finditer(r"@font-face\s*\{[^}]*\}", css_text, re.S):
        block = block_match.group(0)
        urls += re.findall(r'url\(["\']?([^"\')]+)["\']?\)', block)
    return urls


def test_miniapp_font_face_only_references_local_app_static_fonts():
    text = MINIAPP_APP_CSS.read_text(encoding="utf-8")
    urls = _font_face_urls(text)
    assert urls, "нет ни одного @font-face в miniapp/static/app.css"
    for url in urls:
        assert url.startswith("/app/static/fonts/"), f"неожиданный путь шрифта: {url}"
        assert "http" not in url


def test_dashboard_font_face_only_references_local_static_fonts():
    text = DASHBOARD_APP_CSS.read_text(encoding="utf-8")
    urls = _font_face_urls(text)
    assert urls, "нет ни одного @font-face в dashboard/static/app.css"
    for url in urls:
        assert url.startswith("/static/fonts/"), f"неожиданный путь шрифта: {url}"
        assert "http" not in url


# ── icons.js: инвентарь, отсутствие литеральных цветов и innerHTML (D-13) ───────────────────

def test_icons_js_exports_exactly_expected_icon_names():
    text = ICONS_JS.read_text(encoding="utf-8")
    names = set(re.findall(r'^\s*"([a-z0-9-]+)":\s*\[', text, re.M))
    assert names == EXPECTED_ICON_NAMES, (
        f"расхождение с инвентарём UI-SPEC: лишние {names - EXPECTED_ICON_NAMES}, "
        f"не хватает {EXPECTED_ICON_NAMES - names}"
    )
    assert len(EXPECTED_ICON_NAMES) == 26


_FILL_OR_STROKE_ATTR = re.compile(r'\b(?:fill|stroke):\s*"([^"]*)"')


def test_icons_js_only_allows_none_or_currentcolor_for_fill_and_stroke():
    text = ICONS_JS.read_text(encoding="utf-8")
    values = set(_FILL_OR_STROKE_ATTR.findall(text))
    # Атрибуты fill/stroke внутри ICONS-описаний элементов (path/circle/rect) — если такой
    # атрибут вообще задан геометрией, значение обязано быть "none" либо "currentColor";
    # реальная покраска идёт через SVG-атрибуты корня в icon(), не хранится в ICONS.
    assert values <= {"none", "currentColor"}, f"неожиданный fill/stroke: {values}"
    assert 'setAttribute("stroke", "currentColor")' in text
    assert 'setAttribute("fill", "none")' in text


def test_icons_js_has_no_innerhtml_and_no_network_urls():
    text = ICONS_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "document.write" not in text
    # Единственный "http"-литерал в файле — XML-неймспейс SVG (константа DOM API, не фетчится).
    assert "createElementNS" in text
    http_literals = re.findall(r'https?://[^\s"\')`]+', text)
    assert http_literals == ["http://www.w3.org/2000/svg"], http_literals
