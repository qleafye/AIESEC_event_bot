"""Backlog п.25: Mini App меряет загрузку файла со своей стороны (`performance.now()` вокруг fetch
внутри `api()`) и fire-and-forget шлёт `POST /app/api/uploads/timing` с `X-Upload-Ref` ответа.
Вызывающий экран получает ровно тот же результат/исключение, что и раньше; отчёт не идёт через
`api()` и не дёргает экран «Нет доступа». Гоняется в node с подменённым fetch."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import shutil
import subprocess

import pytest

ROOT = REPO_ROOT
API_JS = ROOT / "miniapp" / "static" / "js" / "api.js"

NODE_SCRIPT = """
globalThis.window = { Telegram: { WebApp: { initData: "init-data-x" } } };
let unhandled = false;
process.on("unhandledRejection", () => { unhandled = true; });
let calls = [];
let scenario = null;
let timingMode = "ok";
const jsonResp = (status, body, ref) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (name) => (name === "X-Upload-Ref" ? ref : "application/json") },
  json: async () => body,
});
globalThis.fetch = (url, opts) => {
  calls.push({ url, method: opts.method, keepalive: opts.keepalive === true,
               headers: opts.headers, credentials: opts.credentials,
               body: typeof opts.body === "string" ? opts.body : null });
  if (url === "/app/api/uploads/timing") {
    if (timingMode === "throw") throw new Error("sync boom");
    if (timingMode === "reject") return Promise.reject(new Error("async boom"));
    return Promise.resolve({ ok: true, status: 204, headers: { get: () => null } });
  }
  switch (scenario) {
    case "ok": return Promise.resolve(jsonResp(200, { kind: "document", content: "F" }, "abcdef012345"));
    case "badref": return Promise.resolve({ ok: true, status: 200,
      headers: { get: () => "application/json" }, json: async () => ({ x: 1 }) });
    case "400": return Promise.resolve(jsonResp(400, { reason: "file_rejected" }, "0123456789ab"));
    case "401": return Promise.resolve(jsonResp(401, { reason: "bad_initdata" }, "0123456789ab"));
    case "net": return Promise.reject(new TypeError("Failed to fetch"));
    default: return Promise.resolve(jsonResp(200, { ok: 1 }, null));
  }
};
const m = await import(%(url)s);
m.setAuthErrorHandler(() => {});
const tick = () => new Promise((ok) => setTimeout(ok, 20));
const form = () => { const f = new FormData(); f.append("file", new Blob(["hello"]), "cv.pdf"); return f; };
const timingCalls = () => calls.filter((c) => c.url === "/app/api/uploads/timing")
  .map((c) => ({ ...c, body: JSON.parse(c.body) }));
const r = {};

async function run(name, path, opts) {
  calls = []; scenario = name;
  let out;
  try { out = { value: await m.api(path, opts) }; }
  catch (e) { out = { error: { status: e.status, reason: e.reason, message: e.message,
                               isApiError: e instanceof m.ApiError } }; }
  await tick();
  return { out, n: calls.length, first: calls[0] && calls[0].url, timing: timingCalls() };
}

r.ok = await run("ok", "/uploads?target=resume", { method: "POST", form: form() });
r.badref = await run("badref", "/uploads", { method: "POST", form: form() });
r.rejected = await run("400", "/uploads", { method: "POST", form: form() });
r.net = await run("net", "/uploads?target=task_cover", { method: "POST", form: form() });
r.unauth = await run("401", "/uploads", { method: "POST", form: form() });
r.limits = await run("ok", "/uploads/limits", {});
r.tasks = await run("ok", "/tasks", {});
r.timingPath = await run("ok", "/uploads/timing", { method: "POST", body: {} });
timingMode = "throw";
r.syncThrow = await run("ok", "/uploads", { method: "POST", form: form() });
timingMode = "reject";
r.asyncReject = await run("ok", "/uploads", { method: "POST", form: form() });
await tick();
r.unhandled = unhandled;
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


def test_successful_upload_reports_timing(result):
    r = result["ok"]
    assert r["out"] == {"value": {"kind": "document", "content": "F"}}
    assert r["n"] == 2 and r["first"] == "/app/api/uploads?target=resume"
    (t,) = r["timing"]
    assert t["method"] == "POST" and t["keepalive"] is True
    assert t["credentials"] == "same-origin"
    assert t["headers"]["X-Requested-With"] == "fetch"
    assert t["headers"]["Content-Type"] == "application/json"
    assert t["headers"]["X-Telegram-Init-Data"] == "init-data-x"
    body = t["body"]
    assert body["ref"] == "abcdef012345" and body["status"] == 200
    assert body["target"] == "resume" and body["size"] == 5
    assert isinstance(body["client_ms"], int) and body["client_ms"] >= 0


def test_default_target_and_bad_ref(result):
    r = result["badref"]
    assert r["out"] == {"value": {"x": 1}}
    (t,) = r["timing"]
    assert t["body"]["ref"] is None and t["body"]["target"] == "task"


def test_rejected_upload_still_raises_and_reports_status(result):
    r = result["rejected"]
    assert r["out"]["error"]["status"] == 400 and r["out"]["error"]["reason"] == "file_rejected"
    assert r["out"]["error"]["isApiError"] is True
    (t,) = r["timing"]
    assert t["body"]["status"] == 400 and t["body"]["ref"] == "0123456789ab"


def test_network_failure_reraises_and_reports_zero(result):
    r = result["net"]
    assert r["out"]["error"]["message"] == "Failed to fetch"
    assert r["out"]["error"]["isApiError"] is False
    (t,) = r["timing"]
    assert t["body"]["status"] == 0 and t["body"]["ref"] is None
    assert t["body"]["target"] == "task_cover"


def test_unauthorized_upload_sends_no_report(result):
    r = result["unauth"]
    assert r["out"]["error"]["status"] == 401
    assert r["n"] == 1 and r["timing"] == []


def test_other_requests_are_not_timed(result):
    for key in ("limits", "tasks", "timingPath"):
        assert result[key]["n"] == 1, key


def test_report_failures_do_not_break_upload(result):
    for key in ("syncThrow", "asyncReject"):
        assert result[key]["out"] == {"value": {"kind": "document", "content": "F"}}, key
    assert result["unhandled"] is False
