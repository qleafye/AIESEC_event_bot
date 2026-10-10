"""Сканер Mini App не теряет QR следующего делегата, пока идёт отправка предыдущего.

Утренний поток на медленной сети: волонтёр отсканировал А, ответ ещё в пути, в кадре уже Б.
Раньше занятость снималась только после ответа на скан И загрузки двух счётчиков, а текст Б
запоминался до проверки занятости — Б выбрасывался и потом глушился как «повтор», делегат
проходил неотмеченным. Логика приёма — `miniapp/static/js/scan_gate.js`, гоняется в node
(тот же приём, что `tests/test_miniapp_net_health_js_260925.py`); подключение к экрану —
статикой по `scanner.js`."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = REPO_ROOT
GATE_JS = ROOT / "miniapp" / "static" / "js" / "scan_gate.js"
SCANNER_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"

NODE_SCRIPT = """
const m = await import(%(url)s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const r = {};

// Поток: 10 делегатов по 150 мс в кадре, камера отдаёт текст каждые 20 мс, ответ на скан 120 мс.
{
  const marked = [];
  const g = m.createScanGate({ guardMs: 300, submit: async (t) => { await sleep(120); marked.push(t); } });
  for (let i = 0; i < 10; i++) {
    const end = Date.now() + 150;
    while (Date.now() < end) { g.onText("D" + i); await sleep(20); }
  }
  await sleep(600);
  r.stream = marked;
}

// Ответ дольше окна глушилки, а QR всё ещё в кадре — повторной отправки нет.
{
  const marked = [];
  const g = m.createScanGate({ guardMs: 100, submit: async (t) => { await sleep(250); marked.push(t); } });
  const end = Date.now() + 320;
  while (Date.now() < end) { g.onText("A"); await sleep(15); }
  await sleep(300);
  r.slowSame = marked;
}

// Пока A в пути, B и C пойманы по многу раз — в очереди по одному, порядок сохранён.
{
  const marked = [];
  const g = m.createScanGate({ guardMs: 300, submit: async (t) => { await sleep(60); marked.push(t); } });
  g.onText("A"); g.onText("B"); g.onText("B"); g.onText("C"); g.onText("B"); g.onText("A");
  r.queued = g.pending();
  r.busy = g.isBusy();
  await sleep(300);
  r.order = marked;
  r.idle = !g.isBusy();
}

// Ошибка отправки не вешает приём.
{
  const marked = [];
  const g = m.createScanGate({ guardMs: 50, submit: async (t) => { await sleep(10); if (t === "X") throw new Error("net"); marked.push(t); } });
  g.onText("X"); g.onText("Y");
  await sleep(100);
  g.onText("Z");
  await sleep(100);
  r.afterError = marked;
}

// Колбэк камеры всегда возвращает false — попап закрывает экран сам.
{
  const g = m.createScanGate({ submit: async () => {} });
  r.returns = [g.onText("A"), g.onText("A"), g.onText("B")];
}
// 🔴 на D1 при D2 в очереди: D2 не уходит сам (плашку отказа не затирает), уходит по resume().
{
  const marked = [];
  const g = m.createScanGate({ guardMs: 300, submit: async (t) => {
    await sleep(40); marked.push(t); return t === "D1" ? m.HOLD : undefined;
  } });
  g.onText("D1"); g.onText("D2");
  await sleep(150);
  r.heldMarked = marked.slice();
  r.heldFlag = g.isHeld();
  r.heldPending = g.pending();
  g.onText("D3"); // камера ещё не закрылась — в очередь, не в отправку
  await sleep(100);
  r.heldStill = marked.slice();
  g.resume();
  await sleep(200);
  r.afterResume = marked;
  r.resumedFlag = g.isHeld();
}

// Два QR чередуются в кадре (A уходит, B подходит): ни один не уходит второй раз.
{
  const marked = [];
  const g = m.createScanGate({ guardMs: 400, submit: async (t) => { await sleep(80); marked.push(t); } });
  const end = Date.now() + 300;
  let i = 0;
  while (Date.now() < end) { g.onText(i++ %% 2 ? "A" : "B"); await sleep(15); }
  await sleep(200);
  r.alternating = marked;
}
console.log(JSON.stringify(r));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест scan_gate пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(GATE_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_delegate_of_the_stream_is_marked_once(result):
    assert result["stream"] == [f"D{i}" for i in range(10)]


def test_slow_answer_does_not_resend_same_qr(result):
    assert result["slowSame"] == ["A"]


def test_other_qr_waits_in_queue_without_repeats(result):
    assert result["busy"] is True
    assert result["queued"] == ["B", "C"]
    assert result["order"] == ["A", "B", "C"]
    assert result["idle"] is True


def test_failed_submit_does_not_jam_the_gate(result):
    assert result["afterError"] == ["Y", "Z"]


def test_camera_callback_never_closes_popup_itself(result):
    assert result["returns"] == [False, False, False]


def test_scanner_wires_gate_and_does_not_await_counters_in_scan():
    text = SCANNER_JS.read_text(encoding="utf-8")
    assert 'from "../scan_gate.js"' in text
    assert "createScanGate({ submit: submitScan })" in text
    assert "return HOLD;" in text and "scanGate.resume()" in text
    body = text[text.index("async function submitScan"):text.index("const scanGate")]
    assert "await loadStats" not in body and "await loadPoints" not in body
    assert "refreshCounters()" in body
    assert "scanBusy" not in text


def test_refusal_holds_queue_until_volunteer_continues(result):
    assert result["heldMarked"] == ["D1"]
    assert result["heldFlag"] is True
    assert result["heldPending"] == ["D2"]
    assert result["heldStill"] == ["D1"]
    assert result["afterResume"] == ["D1", "D2", "D3"]
    assert result["resumedFlag"] is False


def test_two_alternating_qrs_are_not_resent(result):
    assert sorted(result["alternating"]) == ["A", "B"]
