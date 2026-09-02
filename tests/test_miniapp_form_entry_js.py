"""Phase 21 (gap closure, D-01/D-24/D-25): структурные сторожа входа в анкету Mini App.

- `screens/form.js`: развилки город/формат участия — интерактивные пикеры, построенные из
  ответа сервера (`pre_items[i].field/text/options`); экран не знает ни имён полей PATCH,
  ни типов развилок, ни человеческих текстов; после PATCH состояние пересобирается целиком
  (выбор трека меняет список шагов).
- `app.js` / `screens/hub.js` (Task 4): плитка «Анкета» видна по `me.form_access`, дом
  приложения — `#/form` при `me.form_first`; хаб для не-делегата с доступной анкетой —
  только плитки.

Хелперы — из read-only `tests/test_miniapp_frontend.py` (импорт, не правка).
"""
from __future__ import annotations

import re

from tests.test_miniapp_frontend import (
    APP_JS,
    SCREENS_DIR,
    _HEX_OR_RGB_COLOR,
    _STRING_LITERAL,
    _js_without_comments,
)

FORM_SCREEN_JS = SCREENS_DIR / "form.js"
HUB_JS = SCREENS_DIR / "hub.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ── screens/form.js: развилки из ответа сервера (Task 3) ─────────────────────────────────

def test_form_screen_forks_use_server_field_and_options():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "item.field" in text
    assert "item.options" in text
    assert "[item.field]" in text
    assert 'method: "PATCH"' in text
    for literal in ('"event_city"', '"participant_type"', '"city_fork"', '"party_fork"'):
        assert literal not in text, literal


def test_form_screen_still_has_no_human_literals_or_innerhtml():
    text = _js_without_comments(FORM_SCREEN_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)
    assert "innerHTML" not in text
    assert not _HEX_OR_RGB_COLOR.search(text)
    assert "https://" not in text


def test_form_screen_pre_screens_rebuilt_from_pre_items():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "preScreens" in text
    assert "pre_items" in text
    # Ветка успеха PATCH развилки пересобирает состояние целиком, не applyServer.
    fork = text[text.index("function drawFork("):text.index("function drawPre(")]
    assert "buildFormState(" in fork or "adoptDraft(" in fork
    assert "applyServer" not in fork
    adopt = text[text.index("function adoptDraft("):text.index("function drawFork(")]
    assert "buildFormState(" in adopt
