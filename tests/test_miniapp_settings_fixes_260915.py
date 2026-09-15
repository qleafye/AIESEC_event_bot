"""Квик 260915-skg, Задача 3 (P2/P4/P5/P6): экран настроек Mini App — матрица «трек × вопрос»
отдаёт строку "on"/"off" вместо bool (колонка «Полная» была вечно пустой), дефолт счётчика
группы читается как «настроек», поиск ведёт на саму настройку, `GET /admin/settings/all` —
один на открытие экрана.

Серверная часть — обычный `asyncio.run()` (в окружении нет pytest-asyncio, см. шапку
`tests/test_reg_composite_edu_260915.py`). Клиентская часть — структурные regex-проверки по
исходнику `screens/settings.js`/`app.js` (без `node`, тот же приём, что
`tests/test_miniapp_form_types_js_260912.py`)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from config import config
from database.db import init_db, set_setting

from miniapp.routers.settings import _reg_questions_matrix
from settings_schema import SETTINGS_SCHEMA

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "settings.js"
APP_JS = ROOT / "miniapp" / "static" / "js" / "app.js"


def _ready(tmp_path, name="miniapp_settings_fixes_260915.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(init_db())


def _row_for(rows, step_key):
    return next(r for r in rows if r["step_key"] == step_key)


# ── (2) матрица «трек × вопрос»: full/party строкой "on"/"off", не bool ────────────────────

def test_matrix_full_on_gives_string_on_and_party_inherits_it(tmp_path):
    _ready(tmp_path)
    asyncio.run(set_setting("reg_q_age", "on"))
    matrix = asyncio.run(_reg_questions_matrix())
    row = _row_for(matrix["rows"], "age")
    assert row["full"]["value"] == "on"
    assert row["party"]["value"] == "on"
    assert row["party"]["is_inherited"] is True


def test_matrix_full_off_gives_string_off_in_both_columns(tmp_path):
    _ready(tmp_path)
    asyncio.run(set_setting("reg_q_age", "off"))
    matrix = asyncio.run(_reg_questions_matrix())
    row = _row_for(matrix["rows"], "age")
    assert row["full"]["value"] == "off"
    assert row["party"]["value"] == "off"
    assert row["party"]["is_inherited"] is True


def test_matrix_explicit_party_override_wins_and_is_not_inherited(tmp_path):
    _ready(tmp_path)
    asyncio.run(set_setting("reg_q_age", "on"))
    asyncio.run(set_setting("reg_q_age__party", "off"))
    matrix = asyncio.run(_reg_questions_matrix())
    row = _row_for(matrix["rows"], "age")
    assert row["full"]["value"] == "on"
    assert row["party"]["value"] == "off"
    assert row["party"]["is_inherited"] is False


def test_matrix_short_column_shape_unaffected(tmp_path):
    """short — уже была строковой (07-01/SHORT-03), фикс full/party не сдвигает её форму."""
    _ready(tmp_path)
    matrix = asyncio.run(_reg_questions_matrix())
    row = _row_for(matrix["rows"], "age")
    assert row["short"]["value"] in ("on", "off")
    assert row["short"]["is_inherited"] is True


# ── (4) подпись счётчика группы — «настроек», не «вопросов» ────────────────────────────────

def test_group_count_default_names_settings_not_questions():
    default = SETTINGS_SCHEMA["miniapp_settings_group_count_text"]["default"]
    assert "{shown}" in default
    assert "{total}" in default
    assert "в разделе" in default


# ── (5/6) клиентская часть — структурные regex-проверки без node ───────────────────────────

def _js_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_js_defines_isOn_normalizer_for_matrix_values():
    text = _js_text(SETTINGS_JS)
    assert "function isOn(value)" in text
    assert 'value === "on" || value === true' in text


def test_settings_js_search_result_navigates_to_setting_not_section():
    text = _js_text(SETTINGS_JS)
    assert "`#/settings/${sectionToken}/${item.key}`" in text


def test_settings_js_render_reads_params_key_for_section_route():
    text = _js_text(SETTINGS_JS)
    assert "renderSection(root, params.code, ctx, params.key || null)" in text


def test_app_js_route_table_has_settings_code_key_pattern():
    text = _js_text(APP_JS)
    assert '["#/settings/{code}/{key}", "screens/settings.js"]' in text


def test_settings_js_has_single_flight_guard_for_settings_all():
    text = _js_text(SETTINGS_JS)
    assert "let allInFlight = null;" in text
    # ровно два литеральных вызова GET /admin/settings/all — по одному на renderStart/
    # renderSection.loadAndRender (T-skg P6), оба под guard'ом allInFlight.
    assert len(re.findall(r'api\("/admin/settings/all"\)', text)) == 2
    assert text.count("if (!allInFlight) allInFlight = api(\"/admin/settings/all\")") == 2


def test_settings_js_save_toggle_does_not_reload_registry_on_stale_or_saved_without_items():
    """Регресс P6: `saveToggle` больше не падает в безусловный `await loadAndRender()` для
    stale/needs_confirm/«сохранено, но без items[0]» — эти три исхода красят строку сами."""
    text = _js_text(SETTINGS_JS)
    assert "resp.stale" in text
    assert "resp.needs_confirm" in text
    assert "matrixCellPaint.get(item.key)(nextValue)" in text
