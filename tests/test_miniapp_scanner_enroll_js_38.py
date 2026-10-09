"""Кнопка «Перезаписать на эту» на плашке сканера: поведение в node + подключение в scanner.js."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENROLL_JS = ROOT / "miniapp" / "static" / "js" / "scanner_enroll.js"
SCANNER_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"

NODE_SCRIPT = """
const m = await import(%(url)s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function h(tag, attrs) {
  const el = { tag, attrs, disabled: false, handlers: {} };
  el.addEventListener = (n, f) => { el.handlers[n] = f; };
  el.hasAttribute = () => el.disabled;
  el.setAttribute = () => { el.disabled = true; };
  el.removeAttribute = () => { el.disabled = false; };
  return el;
}
const res = { telegram_id: 7, enroll: { action: "rebook", session_id: 5, label: "Перезаписать на эту", error: "Не получилось записать — попробуйте ещё раз" } };
const out = { type: typeof m.enrollButton };
{
  const calls = [], said = []; let refreshed = 0;
  const deps = { h, say: (t, k) => said.push([t, k]), failureText: (e, f) => f, refreshCounters: () => { refreshed++; },
    timeoutMs: 1000, api: async (path, o) => { calls.push([path, o]); await sleep(50); return { status: "ok", message: "Записали на «B»" }; } };
  const btn = m.enrollButton(res, deps);
  out.label = btn.attrs.text;
  const p1 = btn.handlers.click(); const p2 = btn.handlers.click();
  await Promise.all([p1, p2]);
  out.calls = calls; out.said = said; out.refreshed = refreshed;
}
{
  const said = [];
  const deps = { h, say: (t, k) => said.push([t, k]), failureText: (e, f) => "ERR:" + f, refreshCounters() {},
    timeoutMs: 1, api: async () => { throw new Error("net"); } };
  const btn = m.enrollButton(res, deps);
  await btn.handlers.click();
  out.errSaid = said; out.errDisabled = btn.disabled;
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = NODE_SCRIPT % {"url": json.dumps(ENROLL_JS.resolve().as_uri())}
    proc = subprocess.run([node, "--input-type=module", "-e", script], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_enroll_button_module_exports(result):
    assert result["type"] == "function"
    assert result["label"] == "Перезаписать на эту"


def test_enroll_button_posts_and_reports(result):
    assert result["calls"] == [["/checkin/enroll", {
        "method": "POST", "timeoutMs": 1000, "body": {"telegram_id": 7, "session_id": 5}}]]
    assert result["said"] == [["Записали на «B»", "ok"]]
    assert result["refreshed"] == 1


def test_enroll_button_failure_reenables(result):
    assert result["errSaid"] == [["ERR:Не получилось записать — попробуйте ещё раз", "warn"]]
    assert result["errDisabled"] is False


def test_scanner_wires_button():
    text = SCANNER_JS.read_text(encoding="utf-8")
    assert 'from "../scanner_enroll.js"' in text
    assert "res.enroll && res.telegram_id && selectedPoint !== TRAINING_POINT" in text
    assert "enrollButton(res," in text
