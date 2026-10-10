"""Слово валюты после числа в Mini App (`unitFor` из `miniapp/static/js/units.js`): подпись «баллов»
из настроек на плите баланса и в рейтинге согласуется с числом — «1 балл», «22 балла». Своё
слово менеджера, которое бот не знает, возвращается как есть. Тот же приём запуска в node, что
у `tests/test_ui_js.py`."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = REPO_ROOT
UI_JS = ROOT / "miniapp" / "static" / "js" / "units.js"

CASES = [
    [1, "баллов", "балл"], [3, "баллов", "балла"], [5, "баллов", "баллов"], [21, "баллов", "балл"],
    [12, "балл", "баллов"], [0, "баллов", "баллов"], [1, "монет", "монета"], [2, "монет", "монеты"],
    [1, "points", "point"], [7, "points", "points"], [1, "лайков", "лайков"], [1, "", ""],
]

NODE_SCRIPT = """
const m = await import(%(url)s);
const cases = %(cases)s;
console.log(JSON.stringify(cases.map(([n, unit]) => m.unitFor(n, unit))));
"""


def test_unit_for_agrees_with_number():
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = NODE_SCRIPT % {
        "url": json.dumps(UI_JS.resolve().as_uri()),
        "cases": json.dumps(CASES, ensure_ascii=False),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got == [c[2] for c in CASES]
