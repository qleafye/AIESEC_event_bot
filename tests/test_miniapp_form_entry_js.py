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


def test_form_screen_progress_label_from_server_template_not_hardcoded():
    """UAT 21-12 находка 2: подпись прогресса «N из M» — шаблон с сервера
    (`d.progress_text`, реестр `reg_form_progress_text`), подстановка {step}/{total} через
    `.replace` (Pitfall 11 — `.format` падает на «{» в тексте менеджера); экран не собирает
    подпись сам строкой `${...}/${...}`."""
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "d.progress_text" in text
    assert '.replace("{step}"' in text
    assert '.replace("{total}"' in text
    assert "}/${" not in text  # старый литеральный формат "N/M" в шаблонной строке


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

# ── app.js / hub.js: плитка анкеты и дом приложения (Task 4, D-24/D-08) ──────────────────

def _function_body(text: str, name: str) -> str:
    start = text.index(name)
    rest = text[start + 1:]
    ends = [m.start() for m in re.finditer(r"^(export )?(async )?function ", rest, re.M)]
    return rest[:ends[0]] if ends else rest


def test_app_js_form_tile_visible_by_form_access_and_home_is_form_when_form_first():
    text = _js_without_comments(APP_JS)
    visible = _function_body(text, "export function visibleNav()")
    assert "me.form_access" in visible
    home = _function_body(text, "function homeHash()")
    assert "me.form_first" in home
    assert '"#/form"' in home
    nav_block = text[text.index("export const NAV = ["):]
    nav_block = nav_block[:nav_block.index("];")]
    assert "form_access" not in nav_block
    assert "formGate" not in nav_block


def test_hub_renders_tiles_only_for_non_delegate_with_form():
    text = _js_without_comments(HUB_JS)
    assert "renderTilesOnlyHub" in text
    assert "form_status_label" in text
    body = _function_body(text, "async function renderHub(")
    assert "delegateItems" in body
    assert "renderTilesOnlyHub(" in body
    assert body.index("renderTilesOnlyHub(") < body.index("renderManagerHub(")
    assert "innerHTML" not in text
    assert not _HEX_OR_RGB_COLOR.search(text)
    assert "https://" not in text


# ── D1 (quick 260904-de4): подсказка формата не задваивается на плите мастера ────────────

def test_wizard_field_call_suppresses_duplicate_help():
    """Плита мастера (`plate-sub`) уже рисует `spec.help`; `field()` рисует то же самое как
    `.field-help` ВСЕГДА (см. `form.js::field`) — второй одинаковый абзац на экране, если не
    погасить подсказку в спеке, отдаваемой полю."""
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "{ ...spec, help: null }" in text


# ── D9 (quick 260904-de4): резюме файлом реально загружается ────────────────────────────

def test_form_screen_uploads_resume_file_directly():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "target=resume" in text
    assert "instanceof File" in text
    assert "markServerDirty" in text


def test_wizard_go_next_never_puts_file_into_json_patch():
    """`JSON.stringify(File)` даёт `{}` — до этой правки мастер клал `liveValue` (сырой File)
    прямо в `patch[column]`, теперь файл-ветка пропускает и `state.setValue`, и `patch[column]`
    (уже отработал `uploadResume`/`markServerDirty` в колбэке выше)."""
    text = _js_without_comments(FORM_SCREEN_JS)
    start = text.index("async function goNext(")
    end = text.index("function goBack(", start)
    body = text[start:end]
    assert "isFileValue" in body
    assert "instanceof File" in body

