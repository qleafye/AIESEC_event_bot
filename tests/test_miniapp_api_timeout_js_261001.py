"""Запросы сканера не висят бесконечно: `api(..., {timeoutMs})` обрывает fetch через
AbortController и бросает ApiTimeout без HTTP-статуса — экран сканера показывает волонтёру
«нет ответа — отсканируйте ещё раз» и снова принимает QR. Гоняется в node с подменённым fetch."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
API_JS = ROOT / "miniapp" / "static" / "js" / "api.js"
SCANNER_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"

NODE_SCRIPT = """
globalThis.window = {};
const calls = [];
globalThis.fetch = (url, opts) => new Promise((ok, fail) => {
  calls.push(Boolean(opts.signal));
  if (url.includes("/fast")) {
    ok({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ x: 1 }) });
    return;
  }
  if (opts.signal) opts.signal.addEventListener("abort", () => fail(new Error("AbortError")));
});
const m = await import(%(url)s);
const r = {};
const started = Date.now();
try { await m.api("/hang", { method: "POST", timeoutMs: 80 }); r.hang = "resolved"; }
catch (e) { r.hang = { timeout: e.timeout === true, isTimeout: e instanceof m.ApiTimeout, status: typeof e.status }; }
r.elapsedOk = Date.now() - started < 2000;
r.fast = await m.api("/fast", { timeoutMs: 80 });
await new Promise((ok) => setTimeout(ok, 150));   // таймер быстрого запроса снят — ничего не падает
r.noSignalWithoutTimeout = (await m.api("/fast"), calls[calls.length - 1] === false);
console.log(JSON.stringify(r));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест api.js пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(API_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_hung_request_is_aborted_as_timeout_without_status(result):
    assert result["hang"] == {"timeout": True, "isTimeout": True, "status": "undefined"}
    assert result["elapsedOk"] is True


def test_fast_request_unaffected(result):
    assert result["fast"] == {"x": 1}
    assert result["noSignalWithoutTimeout"] is True


def test_scanner_uses_timeout_for_scan_and_manual_mark():
    text = SCANNER_JS.read_text(encoding="utf-8")
    assert "const SCAN_TIMEOUT_MS = 7000;" in text
    scan = text[text.index('api("/checkin/scan"'):][:200]
    manual = text[text.index('api("/checkin/manual"'):][:200]
    assert "timeoutMs: SCAN_TIMEOUT_MS" in scan
    assert "timeoutMs: SCAN_TIMEOUT_MS" in manual
    assert "Нет ответа от сервера — отсканируйте ещё раз" in text
