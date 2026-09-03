"""Фаза 23.1 план 07 (находка ревью shots/after: строки хаба показывали Lucide-иконку И
ведущий эмодзи подписи реестра рядом): поведенческая проверка чистой функции `labelText` из
`miniapp/static/js/ui.js`, запущенной в node (в проекте нет JS-тестраннера; тот же приём, что
`tests/test_swipe_js.py`/`tests/test_settings_search_js.py` — `ui.js` импортируется в голом
node без DOM, `labelText` к `document`/`window` не обращается).

Design rule D-04 («в приложении нет эмодзи-иконок») требует, чтобы строка с отдельной Lucide-
иконкой слева (`flatRow({ icon, title })`) не дублировала эмодзи, которым по конвенции реестра
(`settings_schema.py`/`reg_labels.py`) начинается подпись раздела/вопроса анкеты. Сама подпись
в реестре не меняется — `labelText` снимает ведущий эмодзи-кластер только там, где подпись
летит в DOM рядом с иконкой (hub.js/profile.js/form.js).

Таблица кейсов — реальные подписи из реестра (settings_schema.py/reg_labels.py): одиночный
эмодзи + пробел; составной ZWJ/варианс-селектор («⚙️»); флаг из двух Regional Indicator
(«🇬🇧»); подпись без эмодзи (не трогаем); пустая/`null`/`undefined` подпись (не падаем).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = ROOT / "miniapp" / "static" / "js" / "ui.js"

# key -> (вход, ожидаемый результат). Значения — дословные подписи из settings_schema.py/
# reg_labels.py (кроме none/empty/null/undefined).
CASES = {
    "section_tasks": ("\U0001f3af Задания", "Задания"),
    "section_coins": ("\U0001fa99 Монеты", "Монеты"),
    "section_form": ("\U0001f4dd Анкета", "Анкета"),
    "variation_selector": ("⚙️ Настройки", "Настройки"),
    "flag_regional_indicator": ("\U0001f1ec\U0001f1e7 Англ. язык", "Англ. язык"),
    "email": ("\U0001f4e7 Email", "Email"),
    "no_emoji": ("Просто текст", "Просто текст"),
    "parens_not_stripped": ("Дата (без эмодзи)", "Дата (без эмодзи)"),
    "empty": ("", ""),
    "null": (None, ""),
    "undefined": ("__UNDEFINED__", ""),
}

NODE_SCRIPT = """
const m = await import(%(url)s);
const cases = %(cases)s;
const out = { results: {}, exports: Object.keys(m).sort() };
for (const [name, [input]] of Object.entries(cases)) {
  const arg = input === "__UNDEFINED__" ? undefined : input;
  out.results[name] = m.labelText(arg);
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест labelText пропущен")
    script = NODE_SCRIPT % {
        "url": json.dumps(UI_JS.resolve().as_uri()),
        "cases": json.dumps(CASES, ensure_ascii=False),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_label_text_exported(result):
    assert "labelText" in result["exports"]


@pytest.mark.parametrize("name", list(CASES.keys()))
def test_label_text_strips_only_leading_emoji_cluster(result, name):
    _input, expected = CASES[name]
    assert result["results"][name] == expected, name
