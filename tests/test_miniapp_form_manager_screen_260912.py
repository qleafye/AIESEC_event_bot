"""Phase 30 план 07 задача 2 (A2-08, 30-UI-SPEC.md § «Экран менеджера "Анкета мероприятия"»,
артборд 13): блок «Анкета мероприятия» на экране раздела «📝 Анкета» (`screens/settings.js`).

Структурные сторожа по исходнику (тот же приём, что `test_miniapp_settings_group_head_260912.py`
— читаем JS без комментариев, проверяем порядок/отсутствие литералов/условия, node/jsdom не
нужен), плюс реестровые проверки дефолтов `settings_schema.SETTINGS_SCHEMA`.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from domain.settings.schema import SETTINGS_SCHEMA

from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments

SETTINGS_JS = SCREENS_DIR / "settings.js"
UI_JS = SCREENS_DIR.parent / "ui.js"

# Порядок восьми строк — ЖЁСТКИЙ (30-UI-SPEC.md, артборд 13): Чипы → Поиск в справочнике →
# Образование одним экраном → Повторяемые блоки → Счётчик лимита в заголовке → Экран статуса
# во всю ширину → Настройки в шапке анкеты → Вибрация.
EXPECTED_ORDER = [
    "reg_form_chips",
    "reg_form_lookup_search",
    "reg_form_edu_card",
    "reg_form_repeatable",
    "reg_form_limit_counter",
    "reg_form_status_screen",
    "reg_form_header_settings",
    "reg_form_haptics",
]


def _rows_array_text() -> str:
    text = _js_without_comments(SETTINGS_JS)
    start = text.index("const FORM_MANAGER_ROWS = [")
    end = text.index("];", start)
    return text[start:end]


def test_eight_rows_order_matches_ui_spec_artboard_13():
    body = _rows_array_text()
    positions = [body.index(f'"{key}"') for key in EXPECTED_ORDER]
    assert positions == sorted(positions), "порядок восьми строк разошёлся с артбордом 13"


def test_all_nine_toggle_keys_referenced_exactly_once():
    text = _js_without_comments(SETTINGS_JS)
    assert text.count('"reg_form_v2_enabled"') >= 1
    for key in EXPECTED_ORDER:
        assert text.count(f'"{key}"') == 1, f"{key} должен встречаться в settings.js ровно один раз"


def test_manager_block_on_questions_row_and_form_section():
    """Хаб открывает row-questions. Блок тумблеров там тоже, не только в старом разделе form."""
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("function renderSectionBody(")
    fn_end = text.index("\n  async function ", fn_start + 10)
    body = text[fn_start:fn_end]
    assert 'code === "form"' in body
    assert 'code === "row-questions"' in body
    assert "collectFormManagerItems(payload)" in body
    assert "buildFormManagerBlock" in body


def test_questions_row_collects_form_toggles_from_the_form_section():
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    text = SETTINGS_JS.read_text(encoding="utf-8")
    start = text.index("const FORM_MANAGER_MASTER_KEY")
    end = text.index("const COLLAPSE_PREFIX")
    script = text[start:end] + """
const payload = { sections: [
  { token: "form", toggles: [
    { key: "reg_form_v2_enabled" },
    { key: "reg_form_chips" },
    { key: "reg_form_lookup_search" },
    { key: "reg_form_edu_card" },
    { key: "reg_form_repeatable" },
    { key: "reg_form_limit_counter" },
    { key: "reg_form_status_screen" },
    { key: "reg_form_header_settings" },
    { key: "reg_form_haptics" },
    { key: "registration_mode" },
  ]},
  { token: "reg", toggles: [], groups: [{ token: "reg_prompts", items: [{ key: "start_text" }] }] },
]};
console.log(JSON.stringify([...collectFormManagerItems(payload).keys()]));
"""
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got == ["reg_form_v2_enabled", *EXPECTED_ORDER]


def test_segment_has_no_confirm_box_call():
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("function buildFormManagerBlock(")
    fn_end = text.index("\n  function renderSectionBody(", fn_start)
    body = text[fn_start:fn_end]
    assert "openDangerToggleConfirm" not in body
    assert "confirmBox" not in body


def test_rows_use_formv2text_not_raw_registry_label():
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("function buildFormManagerBlock(")
    fn_end = text.index("\n  function renderSectionBody(", fn_start)
    body = text[fn_start:fn_end]
    assert "formV2Text(" in body
    # Ни один из восьми кодовых ключей не попадает в текст строки как отображаемое значение —
    # единственные места, где строки-ключи встречаются в этой функции, это поиск по
    # `regFormItems`/`FORM_MANAGER_ROWS`, не `h(..., {text: ...})` с кодом внутри.
    assert 'text: "reg_form_' not in body


def test_toggle_click_saves_via_existing_batch_endpoint():
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("async function saveManagerToggle(")
    fn_end = text.index("\n  function buildFormManagerBlock(", fn_start)
    body = text[fn_start:fn_end]
    assert "/admin/settings/batch" in body
    assert "item.key" in body


def test_manager_labels_and_hints_registered_in_schema():
    for row_key, prefix in [
        ("reg_form_chips", "chips"), ("reg_form_lookup_search", "lookup_search"),
        ("reg_form_edu_card", "edu_card"), ("reg_form_repeatable", "repeatable"),
        ("reg_form_limit_counter", "limit_counter"), ("reg_form_status_screen", "status_screen"),
        ("reg_form_header_settings", "header_settings"), ("reg_form_haptics", "haptics"),
    ]:
        assert row_key in SETTINGS_SCHEMA
        label_key = f"reg_form_{prefix}_label_text"
        hint_key = f"reg_form_{prefix}_hint_text"
        assert label_key in SETTINGS_SCHEMA, label_key
        assert hint_key in SETTINGS_SCHEMA, hint_key
        assert SETTINGS_SCHEMA[label_key]["group"] == "reg_prompts"
        assert SETTINGS_SCHEMA[hint_key]["group"] == "reg_prompts"


def test_master_switch_label_and_hint_defaults_are_human():
    assert SETTINGS_SCHEMA["reg_form_v2_master_label_text"]["default"] == "Новая анкета"
    assert "одним нажатием" in SETTINGS_SCHEMA["reg_form_v2_master_hint_text"]["default"]


def test_toggle_row_paints_label_and_hint():
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("function buildFormManagerBlock(")
    fn_end = text.index("\n  function renderSectionBody(", fn_start)
    body = text[fn_start:fn_end]
    assert 'formV2Text(`mgr_${row.text}_label`)' in body
    assert 'formV2Text(`mgr_${row.text}_hint`)' in body
    assert 'class: "ss"' in body


def test_apply_server_texts_does_not_replace_form_v2_map():
    """Полная подмена кеша ответом `/me` стирает `mgr_*` из оболочки."""
    text = _js_without_comments(UI_JS)
    start = text.index("export function applyServerTexts")
    end = text.index("\nexport function ", start + 10)
    body = text[start:end]
    assert "_formV2TextsCache = me.form_v2_texts" not in body
    assert "...shell" in body and "...me.form_v2_texts" in body


def test_apply_server_texts_keeps_manager_toggle_labels():
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = """
globalThis.document = { body: { dataset: {
  formV2Texts: JSON.stringify({
    mgr_chips_label: "Чипы частых значений",
    mgr_chips_hint: "Восемь популярных ответов",
    review_title: "Обзор из оболочки",
  }),
} } };
const ui = await import(%s);
ui.applyServerTexts({ form_v2_texts: { review_title: "Review" } });
console.log(JSON.stringify({
  label: ui.formV2Text("mgr_chips_label"),
  hint: ui.formV2Text("mgr_chips_hint"),
  review: ui.formV2Text("review_title"),
}));
""" % json.dumps(UI_JS.resolve().as_uri() + "?mgr=1")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["label"] == "Чипы частых значений"
    assert got["hint"] == "Восемь популярных ответов"
    assert got["review"] == "Review"
