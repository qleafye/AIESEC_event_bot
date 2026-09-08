"""UAT 07.09 (T-d6t-04) — поведенческая проверка `stepIndexFromKey`/`validationErrors`/
`firstFieldError` из `miniapp/static/js/form.js`, запущенная в node (тот же приём, что
`tests/test_miniapp_form_state_js.py`). Все три функции чистые — фейковый DOM не нужен.

Без node — `pytest.skip` с явной причиной.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORM_JS = ROOT / "miniapp" / "static" / "js" / "form.js"

NODE_SCRIPT = """
const m = await import(%(url)s);

const results = {};

// ── stepIndexFromKey ──────────────────────────────────────────────────────────────────
{
  const specs = [{ key: "age" }, { key: "phone" }, { key: "city" }];
  results.doneMarker = m.stepIndexFromKey(specs, m.STEP_DONE);
  results.unknownKey = m.stepIndexFromKey(specs, "неизвестный");
  results.nullKey = m.stepIndexFromKey(specs, null);
  results.knownKey = m.stepIndexFromKey(specs, "phone");
}

// ── validationErrors ──────────────────────────────────────────────────────────────────
{
  results.errorsFromInvalid400 = m.validationErrors({
    status: 400, reason: "invalid", payload: { errors: { phone: "Не похоже на телефон" } },
  });
  results.errorsFrom409 = m.validationErrors({ status: 409, reason: "already_set", payload: {} });
  results.errorsFrom400NoErrors = m.validationErrors({ status: 400, reason: "invalid", payload: {} });
  results.errorsFromNull = m.validationErrors(null);
}

// ── firstFieldError ───────────────────────────────────────────────────────────────────
{
  results.firstOfTwo = m.firstFieldError({ phone: "a", age: "b" });
  results.firstOfNull = m.firstFieldError(null);
}

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест form.js пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_step_index_from_key_done_marker_is_specs_length(result):
    assert result["doneMarker"] == 3


def test_step_index_from_key_unknown_and_null_default_to_zero(result):
    assert result["unknownKey"] == 0
    assert result["nullKey"] == 0


def test_step_index_from_key_known_key_returns_its_index(result):
    assert result["knownKey"] == 1


def test_validation_errors_extracts_errors_from_400_invalid(result):
    assert result["errorsFromInvalid400"] == {"phone": "Не похоже на телефон"}


def test_validation_errors_null_for_other_codes(result):
    assert result["errorsFrom409"] is None
    assert result["errorsFrom400NoErrors"] is None
    assert result["errorsFromNull"] is None


def test_first_field_error_returns_first_entry(result):
    assert result["firstOfTwo"] == {"column": "phone", "text": "a"}


def test_first_field_error_null_when_no_errors(result):
    assert result["firstOfNull"] is None
