"""Quick 260904-de4 Task 5 — поведенческая проверка `formatCount` из `miniapp/static/js/ui.js`,
запущенная в node (в проекте нет JS-тестраннера, тот же приём, что `tests/test_settings_toggle_js.py`).

`formatCount` — чистая функция, фейкового DOM не требует. Таблица кейсов — из плана: 1 →
«1 изменение», 2/3/4 → «изменения», 5–20 → «изменений» (включая 11–14), 21 → «изменение»,
24 → «изменения»; шаблон без группы форм подставляет число по-старому (регресс не задет).

Без node — `pytest.skip` с явной причиной (как `tests/test_settings_search_js.py`).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = ROOT / "miniapp" / "static" / "js" / "ui.js"

TEMPLATE = "Сохранить {count} {изменение|изменения|изменений}"

NODE_SCRIPT = """
const m = await import(%(url)s);
const template = %(template)s;
const cases = [1, 2, 3, 4, 5, 11, 12, 13, 14, 20, 21, 24];
const results = {};
for (const n of cases) results[n] = m.formatCount(template, n);

results.legacyCount = m.formatCount("Сохранить {count} изменений", 3);
results.legacyN = m.formatCount("{n} настроек", 7);
results.noTemplate = m.formatCount("", 3);

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест formatCount пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(UI_JS.resolve().as_uri()), "template": json.dumps(TEMPLATE)}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("n,expected", [
    (1, "Сохранить 1 изменение"),
    (2, "Сохранить 2 изменения"),
    (3, "Сохранить 3 изменения"),
    (4, "Сохранить 4 изменения"),
    (5, "Сохранить 5 изменений"),
    (11, "Сохранить 11 изменений"),
    (12, "Сохранить 12 изменений"),
    (13, "Сохранить 13 изменений"),
    (14, "Сохранить 14 изменений"),
    (20, "Сохранить 20 изменений"),
    (21, "Сохранить 21 изменение"),
    (24, "Сохранить 24 изменения"),
])
def test_ru_pluralization_table(result, n, expected):
    assert result[str(n)] == expected


def test_legacy_template_without_form_group_still_substitutes_number(result):
    assert result["legacyCount"] == "Сохранить 3 изменений"
    assert result["legacyN"] == "7 настроек"


def test_empty_template_is_empty(result):
    assert result["noTemplate"] == ""
