"""Приёмка 13.10: сканер на точке-сессии.

1. 🟢-скан с предложением записи (`res.enroll`) закрывает попап камеры и держит очередь, как
   🟡 «Уже был»: иначе кнопка «Записать / Перезаписать на эту» оставалась под полноэкранной
   камерой (айпад). Признак — `offersEnroll` из `scanner_enroll.js`, гоняется в node; подключение
   к `submitScan` — статикой по `scanner.js`.
2. «Идёт сейчас» у чипа точки — текстом, не 🔴 (красный кружок читали как ошибку).
3. Исход `/checkin/scan`, `/checkin/manual`, `/checkin/enroll` — строка в логе без ПД."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import logging
import shutil
import subprocess

import pytest

from tests.test_miniapp_checkin_enroll_38 import U, _scan, _setup
from tests.test_miniapp_checkin_260924 import BASE, GAME_MANAGER_ID, _hdr

ROOT = REPO_ROOT
ENROLL_JS = ROOT / "miniapp" / "static" / "js" / "scanner_enroll.js"
GATE_JS = ROOT / "miniapp" / "static" / "js" / "scan_gate.js"
SCANNER_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"

NODE_SCRIPT = """
const e = await import(%(enroll)s);
const g = await import(%(gate)s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const enroll = { action: "book", session_id: 5, label: "Записать на эту" };
const out = {
  withEnroll: e.offersEnroll({ status: "new", telegram_id: 7, enroll }),
  training: e.offersEnroll({ status: "new", telegram_id: 7, enroll }, { training: true }),
  noEnroll: e.offersEnroll({ status: "new", telegram_id: 7 }),
  noTid: e.offersEnroll({ status: "new", enroll }),
  empty: e.offersEnroll(null),
};
// Гейт сканера с решением как в submitScan: 🟢 без записи — дальше, 🟢 с записью — HOLD.
{
  const SUCCESS = new Set(["new", "moved"]);
  const answers = { A: { status: "new", telegram_id: 1 }, B: { status: "new", telegram_id: 2, enroll }, C: { status: "new", telegram_id: 3 } };
  const marked = [], closed = [];
  const gate = g.createScanGate({ guardMs: 50, submit: async (t) => {
    await sleep(20);
    const res = answers[t];
    marked.push(t);
    const holds = !SUCCESS.has(res.status) || e.offersEnroll(res);
    if (holds) { closed.push(t); return g.HOLD; }
    return undefined;
  } });
  gate.onText("A"); gate.onText("B"); gate.onText("C");
  await sleep(200);
  out.markedBeforeResume = marked.slice();
  out.closed = closed;
  out.held = gate.isHeld();
  gate.resume();
  await sleep(100);
  out.markedAfterResume = marked;
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = NODE_SCRIPT % {
        "enroll": json.dumps(ENROLL_JS.resolve().as_uri()),
        "gate": json.dumps(GATE_JS.resolve().as_uri()),
    }
    proc = subprocess.run([node, "--input-type=module", "-e", script], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_offers_enroll_only_with_button(result):
    assert result["withEnroll"] is True
    assert result["training"] is False
    assert result["noEnroll"] is False and result["noTid"] is False and result["empty"] is False


def test_green_with_enroll_holds_queue_like_duplicate(result):
    # A отмечен и очередь пошла дальше; B — 🟢 с кнопкой записи: попап закрыт, C ждёт «дальше».
    assert result["markedBeforeResume"] == ["A", "B"]
    assert result["closed"] == ["B"]
    assert result["held"] is True
    assert result["markedAfterResume"] == ["A", "B", "C"]


def test_submit_scan_closes_popup_for_enroll_offer():
    text = SCANNER_JS.read_text(encoding="utf-8")
    body = text[text.index("async function submitScan"):text.index("const scanGate")]
    assert "offersEnroll(res, { training: selectedPoint === TRAINING_POINT })" in body
    assert "if (holds) closeScanPopup();" in body
    assert "showPlaque(res, { closeButton: holds });" in body
    assert "if (!holds) return undefined;" in body
    # тон отметки не трогаем: new/moved остаются success
    assert 'new: "success"' in text and 'moved: "success"' in text


def test_live_point_chip_is_text_not_red_dot():
    text = SCANNER_JS.read_text(encoding="utf-8")
    chips = text[text.index("function renderPointChips"):text.index("async function loadPoints")]
    assert '"🔴 "' not in chips  # 🔴 упомянут в комментарии — проверяем литерал
    assert '" · идёт сейчас"' in chips
