"""Квик 12.09 (UI-аудит, пункты 2, 3, 9) — анкета объясняет сбой сети, подписывает кнопку
выхода и показывает пояснение шага.

- Пункт 2: `POST /app/api/reg/draft/submit` отдаёт `home_cta` — подпись кнопки выхода на
  терминальном экране «Заявка принята» (была одна иконка без текста и без aria-label).
- Пункт 3: обрыв сети на любом шаге анкеты — общий текст из `data-screen-texts` вместо
  пустой плашки (`errorText(err, "")` -> `failText(err)`).
- Пункт 9: `spec.description` (например `case_optin.description`) рендерится отдельным
  абзацем, а не теряется молча.

Хелперы — из read-only `tests/test_miniapp_frontend.py` (структурная часть) и
`tests/test_miniapp_form.py`/`tests/test_miniapp_routes.py` (серверная часть).
"""
from __future__ import annotations

from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments
from tests.test_miniapp_form import _seed_draft, bot_api, client, db_path  # noqa: F401
from tests.test_miniapp_routes import UNREGISTERED_ID, _hdr

FORM_SCREEN_JS = SCREENS_DIR / "form.js"


# ── структурные: screens/form.js ──────────────────────────────────────────────────────────

def test_form_screen_has_no_empty_error_text_fallback():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert 'errorText(err, "")' not in text
    assert 'screenText("network_error")' in text


def test_form_screen_fail_text_helper_wraps_error_text_with_screen_text():
    text = _js_without_comments(FORM_SCREEN_JS)
    fn_start = text.index("function failText(")
    fn_body = text[fn_start:text.index("\n}", fn_start)]
    assert "errorText(err, screenText(" in fn_body
    # 12 вызовов из аудита переведены на общий хелпер.
    assert text.count("failText(err)") >= 12


def test_render_complete_uses_home_cta_in_aria_label_and_text():
    text = _js_without_comments(FORM_SCREEN_JS)
    complete_start = text.index("function renderComplete(")
    complete_end = text.index("\n  function renderAmbassadorOffer", complete_start)
    body = text[complete_start:complete_end]
    assert 'res.home_cta' in body
    assert '"aria-label": res.home_cta' in body


def test_step_description_renders_as_separate_paragraph():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "step-description" in text
    assert "spec.description" in text


def test_form_screen_still_has_no_cyrillic_literals():
    """Сторож 0-хардкода (tests/test_miniapp_form_entry_js.py) обязан остаться зелёным —
    здесь дублируем узкую проверку на кириллицу в добавленных строках, не весь файл."""
    import re
    text = _js_without_comments(FORM_SCREEN_JS)
    fn_start = text.index("function failText(")
    fn_body = text[fn_start:text.index("\n}", fn_start)]
    assert not re.findall(r"[А-Яа-яЁё]", fn_body)


# ── серверный: POST /app/api/reg/draft/submit ──────────────────────────────────────────────

def test_submit_response_carries_home_cta_with_registry_default(client, bot_api):  # noqa: F811
    from settings_schema import SETTINGS_SCHEMA

    _seed_draft(UNREGISTERED_ID, kind="new", patch={"age": 22, "full_name": "Иван Иванов"})
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["home_cta"] == SETTINGS_SCHEMA["miniapp_form_complete_home_cta_text"]["default"]
