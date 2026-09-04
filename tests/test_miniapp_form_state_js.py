"""Quick 260904-de4 Task 2 (D9) — поведенческая проверка `createFormState` из
`miniapp/static/js/form.js`, запущенная в node (в проекте нет JS-тестраннера, тот же приём,
что `tests/test_settings_toggle_js.py`).

`createFormState` — чистая функция, фейкового DOM не требует: импортируем `form.js` в node и
прогоняем таблицу кейсов из плана (D9): `markServerDirty` помечает колонку «грязной» для кнопки
отправки, но НЕ кладёт её в `collectPatch()` (файл резюме уже уехал на сервер — повторный PATCH
текстом затёр бы его); `applyServer` всё равно принимает свежее серверное значение для такой
колонки (`keepDirty` её не защищает — она не «своя локальная» правка); `reset()` снимает оба
набора dirty.

Без node — `pytest.skip` с явной причиной (как `tests/test_settings_search_js.py`).
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

// 1) markServerDirty делает isDirty true, но collectPatch колонку не содержит.
{
  const state = m.createFormState(
    [{ column: "resume_text" }, { column: "full_name" }],
    { resume_text: null, full_name: "Иванова Мария" },
  );
  state.markServerDirty("resume_text");
  results.isDirtyAfterMark = state.isDirty("resume_text");
  results.patchAfterMark = state.collectPatch();
}

// 2) applyServer после markServerDirty подменяет значение, isDirty остаётся true.
{
  const state = m.createFormState(
    [{ column: "resume_text" }],
    { resume_text: null },
  );
  state.markServerDirty("resume_text");
  const touched = state.applyServer({ resume_text: "x" });
  results.valueAfterApplyServer = state.value("resume_text");
  results.touchedByApplyServer = touched;
  results.isDirtyAfterApplyServer = state.isDirty("resume_text");
}

// 3) reset() снимает и локальный dirty, и серверный.
{
  const state = m.createFormState(
    [{ column: "resume_text" }, { column: "full_name" }],
    { resume_text: null, full_name: "A" },
  );
  state.markServerDirty("resume_text");
  state.setValue("full_name", "B");
  state.reset();
  results.isDirtyResumeAfterReset = state.isDirty("resume_text");
  results.isDirtyFullNameAfterReset = state.isDirty("full_name");
  results.fullNameAfterReset = state.value("full_name");
}

// 4) Обычный local dirty (setValue) по-прежнему уходит в collectPatch — регресс не задет.
{
  const state = m.createFormState([{ column: "full_name" }], { full_name: "A" });
  state.setValue("full_name", "B");
  results.patchWithLocalDirty = state.collectPatch();
}

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест createFormState пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_mark_server_dirty_makes_dirty_but_not_patched(result):
    assert result["isDirtyAfterMark"] is True
    assert result["patchAfterMark"] == {}


def test_apply_server_after_mark_updates_value_and_stays_dirty(result):
    assert result["valueAfterApplyServer"] == "x"
    assert result["touchedByApplyServer"] == ["resume_text"]
    assert result["isDirtyAfterApplyServer"] is True


def test_reset_clears_both_local_and_server_dirty(result):
    assert result["isDirtyResumeAfterReset"] is False
    assert result["isDirtyFullNameAfterReset"] is False
    assert result["fullNameAfterReset"] == "A"


def test_local_dirty_still_flows_into_patch(result):
    assert result["patchWithLocalDirty"] == {"full_name": "B"}
