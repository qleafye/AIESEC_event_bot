"""Квик 12.09 (UI-аудит, пункты 4, 10, 14) — шапка карточки группы настроек Mini App.

- Пункт 4: счётчик группы — «N из M» (реестр `miniapp_settings_group_count_text`), не голое
  число `String(group.items.length)`. M считает тумблеры раздела + items всех групп раздела
  (тумблеры подняты НАД карточками групп, `section.toggles`).
- Пункт 14: группа с той же подписью, что раздел (например «💳 Оплата» внутри раздела
  «Оплата»), не повторяет заголовок под шапкой раздела.
- Пункт 10: плейсхолдер поиска настроек умещается на экране 390px (дефолт реестра короче).

Хелперы — из read-only `tests/test_miniapp_frontend.py` (импорт, не правка).
"""
from __future__ import annotations

from settings_schema import SETTINGS_SCHEMA

from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments

SETTINGS_JS = SCREENS_DIR / "settings.js"


# ── структурные: settings.js ────────────────────────────────────────────────────────────

def test_group_counter_no_longer_bare_items_length():
    text = _js_without_comments(SETTINGS_JS)
    assert 'text: String(group.items.length)' not in text
    assert "miniapp_settings_group_count_text" in text
    assert '.replace("{shown}"' in text
    assert '.replace("{total}"' in text


def test_section_total_counts_toggles_plus_all_group_items():
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("function renderSectionBody(")
    fn_end = text.index("\n  async function ", fn_start + 10)
    body = text[fn_start:fn_end]
    assert "section.toggles.length" in body
    assert "g.items" in body or "group.items" in body


def test_group_head_skips_title_when_group_label_matches_section_label():
    text = _js_without_comments(SETTINGS_JS)
    assert "group.label === section.label" in text
    # aria-label остаётся, для скринридера заголовок не пропадает.
    assert '"aria-label": group.label' in text


# ── реестровые: дефолты ──────────────────────────────────────────────────────────────────

def test_group_count_text_default_has_both_placeholders():
    entry = SETTINGS_SCHEMA["miniapp_settings_group_count_text"]
    assert entry["group"] == "miniapp" and entry["type"] == "text"
    assert "{shown}" in entry["default"]
    assert "{total}" in entry["default"]


def test_search_placeholder_default_fits_390px_field():
    default = SETTINGS_SCHEMA["miniapp_settings_search_placeholder_text"]["default"]
    assert len(default) <= 24, f"плейсхолдер длиной {len(default)} — резервирую 390px-поле"
