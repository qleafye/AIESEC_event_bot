"""Phase 30 план 07 задача 2 (A2-08, 30-UI-SPEC.md § «Экран менеджера "Анкета мероприятия"»,
артборд 13): блок «Анкета мероприятия» на экране раздела «📝 Анкета» (`screens/settings.js`).

Структурные сторожа по исходнику (тот же приём, что `test_miniapp_settings_group_head_260912.py`
— читаем JS без комментариев, проверяем порядок/отсутствие литералов/условия, node/jsdom не
нужен), плюс реестровые проверки дефолтов `settings_schema.SETTINGS_SCHEMA`.
"""
from __future__ import annotations

from settings_schema import SETTINGS_SCHEMA

from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments

SETTINGS_JS = SCREENS_DIR / "settings.js"

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


def test_manager_block_only_for_form_section():
    text = _js_without_comments(SETTINGS_JS)
    fn_start = text.index("function renderSectionBody(")
    fn_end = text.index("\n  async function ", fn_start + 10)
    body = text[fn_start:fn_end]
    assert 'code === "form"' in body
    assert "buildFormManagerBlock" in body


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
