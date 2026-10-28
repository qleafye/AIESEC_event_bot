"""Фронт экрана «🛍 Магазин» Mini App (`miniapp/static/js/screens/shop.js`): статические
сторожа без DOM (в проекте нет запускалки JS-тестов), тот же приём, что
`tests/test_miniapp_program_js_260924.py`."""
from __future__ import annotations

import re

from tests.test_miniapp_frontend import APP_JS, SCREENS_DIR, _HEX_OR_RGB_COLOR, _js_without_comments

SHOP_JS = SCREENS_DIR / "shop.js"

_CYRILLIC_LITERAL = re.compile(r"""(["'`])[^"'`\n]*[А-Яа-яЁё][^"'`\n]*\1""")


def _text() -> str:
    return _js_without_comments(SHOP_JS)


def test_exports_render_via_guarded_render():
    text = _text()
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text)
    assert "guardedRender(" in text


def test_safe_and_self_contained():
    text = _text()
    assert "innerHTML" not in text and "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text)
    assert "https://" not in text and "http://" not in text
    for spec in re.findall(r'from\s+"([^"]+)"', text):
        assert spec.startswith("../"), spec


def test_no_russian_literals():
    """Все подписи — с сервера (`texts`, `note`, `*_text`): EN приходит переведённым."""
    assert not _CYRILLIC_LITERAL.findall(_text())


def test_calls_shop_api():
    text = _text()
    assert 'api("/shop")' in text
    assert 'api("/shop/reserve"' in text and 'method: "POST"' in text and "request_id" in text
    assert re.search(r"`/shop/reservations/\$\{[^}]+\}/cancel`", text)
    assert re.search(r"`/shop/reservations/\$\{[^}]+\}/qr`", text)


def test_server_decides_and_confirms_before_charge():
    text = _text()
    for needle in ("confirm-box", "fileUrl(", "item.can_take", "item.note", "confirm_text",
                   "cancel_confirm_text", "errorText(err", "busy", "shop_closed", "emptyState("):
        assert needle in text, needle


def test_app_js_registers_shop_three_times():
    assert APP_JS.read_text(encoding="utf-8").count('"#/shop"') == 3
