"""Камера не закрывается на «уже был» и на чужой картинке.

Волонтёр на площадке жал «Сканировать» заново после каждого такого скана. Закрытие
остаётся, когда на плашке есть действие или отказ, который надо прочитать.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._paths import REPO_ROOT

FLOW_JS = REPO_ROOT / "miniapp" / "static" / "js" / "scanner_flow.js"
ENROLL_JS = REPO_ROOT / "miniapp" / "static" / "js" / "scanner_enroll.js"
SCANNER_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"

NODE_SCRIPT = """
const f = await import(%(flow)s);
const e = await import(%(enroll)s);
const enroll = { action: "book", session_id: 5, label: "Записать на эту" };
const go = (res) => f.scanContinues(res, { offersEnroll: e.offersEnroll });
const out = {
  marked: go({ status: "new", telegram_id: 1 }),
  moved: go({ status: "moved", telegram_id: 1 }),
  duplicate: go({ status: "duplicate", telegram_id: 1, scanned_at: "2026-10-31 11:00:00" }),
  foreign: go({ status: "foreign_event" }),
  shop: go({ status: "shop", telegram_id: 1 }),
  denied: go({ status: "denied", reason_text: "Заявка ещё на рассмотрении" }),
  notFound: go({ status: "not_found" }),
  wrongCity: go({ status: "wrong_city" }),
  notForumDay: go({ status: "not_forum_day", telegram_id: 1 }),
  dayOverride: go({ status: "not_forum_day", telegram_id: 1, day_override: true }),
  onsite: go({ status: "denied", telegram_id: 1, onsite_approve: true }),
  enroll: go({ status: "new", telegram_id: 1, enroll }),
  duplicateEnroll: go({ status: "duplicate", telegram_id: 1, enroll }),
  network: go({ status: "error" }),
  empty: go(null),
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = NODE_SCRIPT % {
        "flow": json.dumps(FLOW_JS.resolve().as_uri()),
        "enroll": json.dumps(ENROLL_JS.resolve().as_uri()),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_camera_stays_open_for_mark_duplicate_and_foreign(result):
    assert result["marked"] is True
    assert result["moved"] is True
    assert result["duplicate"] is True
    assert result["foreign"] is True
    assert result["shop"] is True


def test_camera_closes_when_the_plaque_needs_a_tap(result):
    assert result["denied"] is False
    assert result["notFound"] is False
    assert result["wrongCity"] is False
    assert result["notForumDay"] is False
    assert result["dayOverride"] is False
    assert result["onsite"] is False
    assert result["enroll"] is False
    assert result["duplicateEnroll"] is False
    assert result["network"] is False
    assert result["empty"] is False


def test_scanner_wires_continue_and_explains_the_vibrations():
    text = SCANNER_JS.read_text(encoding="utf-8")
    assert 'from "../scanner_flow.js"' in text
    assert "Зелёная — отмечен. Жёлтая — уже был." in text
    body = text[text.index("async function submitScan"):text.index("const scanGate")]
    assert "scanContinues(res," in body
    assert "if (holds) closeScanPopup();" in body
