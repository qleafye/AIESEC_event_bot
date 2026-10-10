"""Приёмка 09.10 (Mini App): после сбоя загрузки файла «Написать текстом» не давала подать анкету.

Делегат выбрал «Загрузить файл», загрузка упала, он нажал «Написать текстом» внутри дропзоны и
написал резюме текстом. Тип резюме при этом оставался «файл» (`resume_type=file`, файла нет,
`resume_text` заполнен), а гард подачи пропускал текст только в режиме «файл или текст» —
в развилке делегат получал «Файл резюме ещё не загрузился» и ходил по кругу.

Сервер: текст резюме в черновике — это резюме и в развилке; тип при подаче переписывается на
«текст», чтобы в листе и карточке модерации не стояло «Файл» без файла.
Экран: кнопка «Написать текстом» внутри дропзоны развилки переключает ветку на текст (тот же
переход, что кнопка развилки), а не просто показывает поле.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import subprocess
from pathlib import Path

import pytest

from database import db as bot_db

from tests.test_miniapp_form import (  # noqa: F401 — фикстуры подтягиваются по имени
    bot_api,
    client,
    db_path,
    _run,
    _seed_draft,
)
from tests.test_miniapp_routes import DELEGATE_ID, UNREGISTERED_ID, _hdr, _set
from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_form_controls_js_260911 import _FAKE_DOM_PRELUDE

ROOT = REPO_ROOT
FORM_JS = ROOT / "miniapp" / "static" / "js" / "form.js"
SCREEN_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "form.js"


def _mode(mode: str):
    _set("reg_q_resume", "on")
    _set("reg_resume_mode", mode)


def _submit(client, telegram_id):
    return client.post("/app/api/reg/draft/submit", headers=_hdr(telegram_id))


# ── Сервер ────────────────────────────────────────────────────────────────────────────────

def test_fork_file_branch_with_text_is_submitted_as_text(client, bot_api):
    _mode("fork")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={
        "full_name": "Иван Иванов", "age": 22, "resume_type": "file",
        "resume_file_id": None, "resume_text": "Опыт: вёл проект в АЙСЕК",
    })
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 200, resp.text
    user = _run(bot_db.get_user(UNREGISTERED_ID))
    assert user["resume_type"] == "text"
    assert user["resume_text"] == "Опыт: вёл проект в АЙСЕК"


def test_fork_file_branch_without_text_is_still_refused(client, bot_api):
    _mode("fork")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={
        "full_name": "Иван Иванов", "age": 22, "resume_type": "file", "resume_file_id": None,
    })
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason"] == "resume_file_missing"


def test_fork_file_branch_with_blank_text_is_still_refused(client, bot_api):
    _mode("fork")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={
        "full_name": "Иван Иванов", "age": 22, "resume_type": "file", "resume_text": "   ",
    })
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason"] == "resume_file_missing"


def test_edit_switch_to_text_after_failed_file_passes(client, bot_api):
    _mode("fork")
    _seed_draft(DELEGATE_ID, kind="edit", patch={"resume_type": "file", "resume_text": "мой опыт"})
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text
    assert _run(bot_db.get_user(DELEGATE_ID))["resume_type"] == "text"


# ── Экран: «Написать текстом» внутри дропзоны развилки ───────────────────────────────────

_NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);
function all(node, out = []) {
  for (const c of node.children || []) { if (!c || c.nodeType === 3) continue; out.push(c); all(c, out); }
  return out;
}
const out = {};

// Развилка: у спеки есть переход на ветку «текст» — кнопка зовёт его, поле не раскрывает.
let switched = 0;
const changes = [];
const forkEl = m.field(h, {
  key: "resume", column: "resume_text", type: "file", label: "Резюме",
  text_button_text: "Написать текстом", __onWriteText: () => { switched += 1; },
}, null, (v) => changes.push(v));
const forkNodes = all(forkEl);
const forkToggle = forkNodes.find((n) => n.classList && n.classList.contains("dropzone-toggle-text"));
const forkArea = forkNodes.find((n) => n.tagName === "TEXTAREA");
forkToggle.dispatch("click");
out.forkSwitched = switched;
out.forkAreaHidden = forkArea.classList.contains("hidden");
out.forkChanges = changes.length;

// Режим «файл или текст» (без перехода в спеке) — прежнее поведение: поле раскрывается.
const plainEl = m.field(h, {
  key: "resume", column: "resume_text", type: "file", label: "Резюме", text_button_text: "Написать текстом",
}, null, () => {});
const plainNodes = all(plainEl);
plainNodes.find((n) => n.classList && n.classList.contains("dropzone-toggle-text")).dispatch("click");
out.plainAreaHidden = plainNodes.find((n) => n.tagName === "TEXTAREA").classList.contains("hidden");
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node():
    import shutil
    path = shutil.which("node")
    if not path:
        pytest.skip("node не найден в PATH")
    return path


def test_fork_dropzone_text_button_switches_branch(node, tmp_path):
    script = tmp_path / "run.mjs"
    script.write_text(_NODE_SCRIPT % {"url": json.dumps(FORM_JS.as_uri())}, encoding="utf-8")
    res = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out == {"forkSwitched": 1, "forkAreaHidden": True, "forkChanges": 0, "plainAreaHidden": False}


def test_wizard_file_branch_wires_switch_to_text():
    text = _js_without_comments(SCREEN_JS)
    wizard = text[text.index("async function renderWizard("):]
    sub = wizard[wizard.index('resumeForkBranch === "file"'):]
    sub = sub[:sub.index(": rawSpec;")]
    assert "__onWriteText" in sub
    assert 'pickResumeBranch("text")' in sub


def test_overview_file_branch_wires_switch_to_text():
    text = _js_without_comments(SCREEN_JS)
    fn = text[text.index("function resumeEditSpec(spec)"):]
    fn = fn[:fn.index("function fieldRow(spec)")]
    assert "__onWriteText" in fn
    assert 'pickResumeBranchInOverview("text")' in fn
