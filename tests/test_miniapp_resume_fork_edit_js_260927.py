"""Квик 27.09 — клиентская половина двух багов резюме в Mini App.

A1. Обзор правки в режиме развилки: тап «Файл/Ссылка/Текстом/Нет резюме» клал КОД кнопки в
`resume_text` (общая ветка `liveValue = v` + галка `state.setValue(column, liveValue)`).
Теперь выбор ведёт в свою ветку тем же правилом, что и мастер (`resumeForkPick`), и шлёт
PATCH `resume_type`, а код в значение поля не попадает никогда.

A2. Мастер новой анкеты: «Дальше» не ждала загрузки файла резюме — анкета уходила без него.
Теперь промис загрузки хранится, «Дальше» выключена, пока он в полёте, а `goNext` его ждёт.

Два слоя, как в `tests/test_miniapp_resume_clear_js_260912.py`: node-проверка чистого
хелпера из `static/js/form.js` (без node в PATH — skip) и структурные регэкспы по
`screens/form.js` без комментариев.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import FORM_JS, SCREENS_DIR, _js_without_comments
from tests.test_miniapp_resume_clear_js_260912 import _FAKE_DOM_PRELUDE

ROOT = REPO_ROOT
FORM_SCREEN_JS = SCREENS_DIR / "form.js"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Слой 1: form.js::resumeForkPick в node
# ══════════════════════════════════════════════════════════════════════════════════════════

NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(form_url)s);
const out = {};
out.hasFn = typeof m.resumeForkPick === "function";
if (out.hasFn) {
  for (const code of ["file", "text", "link", "mini", "bogus", "", null]) {
    out[String(code)] = m.resumeForkPick(code);
  }
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def pick() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест resumeForkPick пропущен")
    script = NODE_SCRIPT % {"form_url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_resume_fork_pick_is_exported(pick):
    assert pick["hasFn"] is True


@pytest.mark.parametrize("code", ["file", "text"])
def test_file_and_text_stay_on_step_with_local_branch(pick, code):
    assert pick[code] == {"resumeType": code, "localBranch": code, "staysOnStep": True}


@pytest.mark.parametrize("code", ["link", "mini"])
def test_link_and_mini_move_on_without_local_branch(pick, code):
    assert pick[code] == {"resumeType": code, "localBranch": None, "staysOnStep": False}


@pytest.mark.parametrize("code", ["bogus", "", "null"])
def test_unknown_code_gives_null(pick, code):
    assert pick[code] is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Слой 2: screens/form.js — структурные сторожа
# ══════════════════════════════════════════════════════════════════════════════════════════

def _screen_text() -> str:
    return _js_without_comments(FORM_SCREEN_JS)


def _between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start)]


def test_screen_imports_resume_fork_pick():
    head = _screen_text().split('from "../form.js"', 1)[0]
    assert "resumeForkPick" in head


def test_overview_fork_row_never_writes_code_into_value():
    body = _between(_screen_text(), "function fieldRow(spec)", "function drawList()")
    fork_branch = body.index('spec.type === "resume-fork"')
    # Ветка развилки уводит в pickResumeBranchInOverview и возвращается до общей записи
    # liveValue/галки: между проверкой типа и вызовом выбора ветки нет state.setValue.
    call = body.index("pickResumeBranchInOverview(", fork_branch)
    assert "state.setValue(" not in body[fork_branch:call]
    assert "liveValue = v;" not in body[fork_branch:call]


def test_overview_fork_row_has_no_confirm_button():
    body = _between(_screen_text(), "function fieldRow(spec)", "function drawList()")
    # Кнопка-галка рисуется только для строк, где тап не выбор ветки.
    assert "isForkPick" in body or 'spec.type !== "resume-fork"' in body


def test_overview_pick_sends_resume_type_patch_via_shared_rule():
    text = _screen_text()
    body = _between(text, "async function pickResumeBranchInOverview(", "function fieldRow(spec)")
    assert "resumeForkPick(" in body
    assert "answers: { resume_type" in body
    assert "state.setValue(column, code)" not in body


def test_wizard_pick_uses_same_rule():
    text = _screen_text()
    body = _between(text, "async function pickResumeBranch(code)", "function compositeErrorText(")
    assert "resumeForkPick(" in body
    assert 'code === "file" || code === "text"' not in body


def test_upload_resume_reports_success():
    body = _between(_screen_text(), "async function uploadResume(file, el, ctx)", "async function removeResume(")
    assert "return true" in body
    assert "return false" in body


def test_wizard_keeps_upload_promise_and_waits_for_it():
    text = _screen_text()
    wizard = text[text.index("async function renderWizard("):]
    assert "pendingUpload = null" in wizard
    drawstep = _between(wizard, "function drawStep()", "async function goNext()")
    assert "pendingUpload" in drawstep and "uploadResume(" in drawstep
    go_next = _between(wizard, "async function goNext()", "async function goSkip()")
    wait = go_next.index("await pendingUpload")
    assert wait < go_next.index('api("/reg/draft"')


def test_main_button_disabled_while_upload_in_flight():
    text = _screen_text()
    body = _between(text, "function currentMainDisabled()", "function syncMainButton()")
    assert "pendingUpload" in body


def test_submit_form_returns_to_resume_step_on_missing_file():
    text = _screen_text()
    body = _between(text, "async function submitForm()", "drawCurrent();\n  }\n}")
    assert '"resume_file_missing"' in body
    branch = body[body.index('"resume_file_missing"'):]
    assert 'stepIndexFromKey(state.specs, "resume")' in branch
    assert 'resumeForkBranch = "file"' in branch
