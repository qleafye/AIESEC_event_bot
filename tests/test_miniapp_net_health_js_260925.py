"""Идея №11: индикатор связи в сканере Mini App. Логика «показать/скрыть полосу» —
`miniapp/static/js/net_health.js`, гоняется в node (тот же приём, что
`tests/test_miniapp_form_state_js.py`); подключение к экрану и тексты с сервера — статикой
и через `/app/api/checkin/net-texts`."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_checkin_260924 import BASE, _grant_checkin_to_game_manager, client_with
from tests.test_miniapp_frontend import SCREENS_DIR, _HEX_OR_RGB_COLOR, _js_without_comments
from tests.test_miniapp_routes import DELEGATE_ID, GAME_MANAGER_ID, _hdr

ROOT = Path(__file__).resolve().parent.parent
NET_JS = ROOT / "miniapp" / "static" / "js" / "net_health.js"
SCANNER_JS = SCREENS_DIR / "scanner.js"
_CYRILLIC_LITERAL = re.compile(r"""(["'`])[^"'`\n]*[А-Яа-яЁё][^"'`\n]*\1""")

NODE_SCRIPT = """
const m = await import(%(url)s);
const r = {};
const net = { status: undefined };            // fetch упал до HTTP-статуса
const e500 = { status: 502 };
const e400 = { status: 400 };

{ const h = m.createNetHealth(); r.twoSlow = [h.record(6000), h.record(7000)]; }
{ const h = m.createNetHealth(); r.threeSlow = [h.record(6000), h.record(100, net), h.record(100, e500)]; }
{ const h = m.createNetHealth(); r.midBreaks = [h.record(6000), h.record(6000), h.record(3000), h.record(6000)]; }
{ const h = m.createNetHealth(); r.fast400 = [h.record(6000), h.record(6000), h.record(100, e400)]; }
{
  const h = m.createNetHealth();
  h.record(6000); h.record(6000); h.record(6000);
  r.recover = [h.record(100), h.record(100), h.record(3000), h.record(100), h.record(100), h.record(100)];
}
{
  // висящий запрос засчитан плохим по таймеру, поздний ответ второй раз не считается
  const h = m.createNetHealth({ slowMs: 20 });
  const seen = [];
  let t = 0;
  await m.timed(h, () => new Promise((ok) => setTimeout(() => ok("x"), 60)), (d) => seen.push(d),
    { slowMs: 20, now: () => (t += 1) });
  r.hungCountedOnce = seen.length;
  const errs = [];
  try { await m.timed(h, async () => { throw net; }, (d) => errs.push(d)); } catch (e) { r.rethrown = e === net; }
  r.errCounted = errs.length;
}
console.log(JSON.stringify(r));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест net_health пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(NET_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_two_slow_is_not_enough(result):
    assert result["twoSlow"] == [False, False]


def test_three_bad_in_a_row_mixes_slow_network_error_and_5xx(result):
    assert result["threeSlow"] == [False, False, True]


def test_middle_response_breaks_the_streak(result):
    assert result["midBreaks"] == [False, False, False, False]


def test_fast_4xx_is_not_a_network_problem(result):
    assert result["fast400"] == [False, False, False]


def test_banner_hides_after_three_fast_in_a_row(result):
    assert result["recover"] == [True, True, True, True, True, False]


def test_hung_request_counted_once_and_errors_rethrown(result):
    assert result["hungCountedOnce"] == 1
    assert result["rethrown"] is True
    assert result["errCounted"] == 1


def test_scanner_measures_scan_and_manual_and_has_no_russian_in_banner():
    text = _js_without_comments(SCANNER_JS)
    assert 'from "../net_health.js"' in text
    assert re.search(r'measured\(\(\) => api\("/checkin/scan"', text)
    assert re.search(r'measured\(\(\) => api\("/checkin/manual"', text)
    assert 'api("/checkin/net-texts")' in text
    block = text[text.index("const netText"):text.index("function measured")]
    assert not _CYRILLIC_LITERAL.findall(block)
    net = _js_without_comments(NET_JS)
    assert not _CYRILLIC_LITERAL.findall(net)
    assert not _HEX_OR_RGB_COLOR.findall(net)
    assert "innerHTML" not in text + net


def test_net_texts_endpoint_returns_registry_texts(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    body = client.get(f"{BASE}/net-texts", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["text"].startswith("Сеть медленная")
    assert body["help_label"] == "Как"
    assert "Если на площадке нет сети" in body["help_text"]


def test_net_texts_requires_checkin_cap(tmp_path):
    client = client_with(tmp_path)
    assert client.get(f"{BASE}/net-texts", headers=_hdr(DELEGATE_ID)).status_code == 403
