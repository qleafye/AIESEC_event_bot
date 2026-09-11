"""Quick 260911-5ij (W2 «Ошибки видно») — поведенческие node-тесты общей обёртки первичной
загрузки экрана (`ui.js::guardedRender`/`screenText`/`isCoreHandledError`) и, начиная с
задачи 2, пяти экранов, которые её применяют, и задачи 3 — гейта `submit.js`.

Node-подпроцесс без jsdom — тот же приём, что `tests/test_miniapp_form_controls_js_260911.py`
(в проекте нет общего DOM-хелпера, каждый файл фазы носит свою копию фейкового DOM). Без node
в PATH — `pytest.skip` с явной причиной. `motion.js` (`applyMotionTier`/`countUp`/`confetti`)
не имеет побочных эффектов на верхнем уровне импорта (см. контекст плана, п.11) — экраны
`screens/*.js` импортируются в голый node без `requestAnimationFrame`/`performance`, если
`document.documentElement.dataset.motion` задан заранее.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import _STRING_LITERAL, _js_without_comments

ROOT = Path(__file__).resolve().parent.parent
MINIAPP_JS = ROOT / "miniapp" / "static" / "js"
UI_JS = MINIAPP_JS / "ui.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_ui_js_has_no_cyrillic_string_literal():
    """Пилар 6: подпись кнопки повтора и текст ошибки уехали в реестр — в `ui.js` не должно
    остаться ни одного русского строкового литерала (было ровно одно место — "Повторить")."""
    text = _js_without_comments(UI_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)


_FAKE_DOM_PRELUDE = """
class FakeClassList {
  constructor(el) { this._el = el; this._set = new Set(); }
  add(...names) { for (const n of names) this._set.add(n); this._sync(); }
  remove(...names) { for (const n of names) this._set.delete(n); this._sync(); }
  toggle(name, force) {
    const has = this._set.has(name);
    const next = force === undefined ? !has : Boolean(force);
    if (next) this._set.add(name); else this._set.delete(name);
    this._sync();
    return next;
  }
  contains(name) { return this._set.has(name); }
  _sync() { this._el._className = [...this._set].join(" "); }
  _fromString(v) { this._set = new Set(String(v || "").split(/\\s+/).filter(Boolean)); }
}

class FakeText { constructor(text) { this.nodeType = 3; this.textContent = String(text); } }

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this._attrs = new Map();
    this._className = "";
    this.children = [];
    this._listeners = {};
    this.classList = new FakeClassList(this);
  }
  get className() { return this._className; }
  set className(v) { this._className = v; this.classList._fromString(v); }
  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  removeAttribute(name) { this._attrs.delete(name); }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  dispatch(type, evt) { for (const fn of (this._listeners[type] || []).slice()) fn(evt); }
  appendChild(node) { this.children.push(node); return node; }
  append(...nodes) { for (const n of nodes) if (n != null && n !== false) this.appendChild(n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get textContent() {
    return this.children.filter((c) => c.nodeType === 3).map((c) => c.textContent).join("");
  }
  set textContent(v) { this.children = [new FakeText(v)]; }
  get value() { return this._value === undefined ? "" : this._value; }
  set value(v) { this._value = v; }
  focus() {}
  querySelector(sel) {
    const cls = sel.startsWith(".") ? sel.slice(1) : null;
    const stack = [...this.children];
    while (stack.length) {
      const node = stack.shift();
      if (!node || node.nodeType === 3) continue;
      if (cls && node.classList && node.classList.contains(cls)) return node;
      if (node.children) stack.push(...node.children);
    }
    return null;
  }
}

globalThis.document = {
  createElement(tag) { return new FakeElement(tag); },
  createElementNS(ns, tag) { return new FakeElement(tag); },
  createTextNode(text) { return new FakeText(text); },
  documentElement: { dataset: { motion: "off" } },
  body: { dataset: {} },
};

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  return el;
}

function err(status, reason) {
  const e = new Error(`api ${status} ${reason || ""}`);
  e.status = status;
  if (reason) e.reason = reason;
  return e;
}
"""

_TASK1_SCRIPT = _FAKE_DOM_PRELUDE + """
const SCREEN_TEXTS = {
  load_error: "Тестовый текст ошибки загрузки",
  retry: "Тестовая кнопка повтора",
  submit_pending: "тест-pending",
  submit_approved: "тест-approved",
  submit_limit: "тест-limit",
};
document.body.dataset.screenTexts = JSON.stringify(SCREEN_TEXTS);

const ui1 = await import(%(ui_url)s + "?v=1");

const screenTextKnown = ui1.screenText("load_error");
const screenTextUnknown = ui1.screenText("does_not_exist");

// errorState подписывает кнопку значением screenText("retry"), не литералом.
const errorNode = ui1.errorState(h, { me: {}, text: "боль", retry: () => {} });
const retryBtnInErrorState = errorNode.querySelector(".btn");

const coreCases = {
  c401: ui1.isCoreHandledError(err(401)),
  c403: ui1.isCoreHandledError(err(403)),
  cOffMini: ui1.isCoreHandledError(err(503, "miniapp_off")),
  cDbLocked: ui1.isCoreHandledError(err(503, "db_locked")),
  cTypeError: ui1.isCoreHandledError(new TypeError("network")),
  cNone: ui1.isCoreHandledError(null),
};

// guardedRender: первый draw() бросает ApiError(500) -> состояние ошибки; повтор рисует
// нормальный экран.
const root1 = document.createElement("div");
let calls1 = 0;
async function draw1() {
  calls1++;
  if (calls1 === 1) throw err(500, "server_error");
  root1.append(h("div", { class: "content-ok", text: "готово" }));
}
await ui1.guardedRender(root1, { h, me: {} }, draw1);
const afterFirstErrorNode = root1.querySelector(".error-state");
const afterFirstChildCount = root1.children.length;

const retryBtn1 = afterFirstErrorNode.querySelector(".btn");
await retryBtn1._listeners.click[0]();
const afterRetryErrorNode = root1.querySelector(".error-state");
const afterRetryContentNode = root1.querySelector(".content-ok");
const afterRetryChildCount = root1.children.length;

// guardedRender: повтор, который снова падает — не дописывает второй экран под первым.
const root2 = document.createElement("div");
async function drawAlwaysFails() { throw err(502, "bad_gateway"); }
await ui1.guardedRender(root2, { h, me: {} }, drawAlwaysFails);
const errBtn2 = root2.querySelector(".error-state").querySelector(".btn");
await errBtn2._listeners.click[0]();
const errorNodesAfterDoubleFail = root2.children.filter(
  (c) => c.classList && c.classList.contains("error-state"),
).length;

// guardedRender: 401/403/503+miniapp_off — экран уже покрасило ядро ДО броска (симулируем
// синхронную запись "ядра" внутри draw перед throw, ровно как showState делает это внутри
// api() до того, как та бросит ApiError) — guardedRender не трогает root повторно.
const root3 = document.createElement("div");
async function drawCoreHandled() {
  root3.append(h("section", { class: "state", text: "state-painted-by-core" }));
  throw err(403);
}
await ui1.guardedRender(root3, { h, me: {} }, drawCoreHandled);
const root3ChildCount = root3.children.length;
const root3StateText = root3.children[0] ? root3.children[0].textContent : null;

// screenText: битый JSON -> "" без исключения (свежий инстанс модуля — кэш per-module).
document.body.dataset.screenTexts = "{not valid json";
const ui2 = await import(%(ui_url)s + "?v=2");
const screenTextOnBadJson = ui2.screenText("load_error");

console.log(JSON.stringify({
  screenTextKnown, screenTextUnknown,
  retryBtnLabel: retryBtnInErrorState ? retryBtnInErrorState.textContent : null,
  coreCases,
  afterFirstChildCount, hasErrorAfterFirst: Boolean(afterFirstErrorNode),
  afterRetryChildCount, hasErrorAfterRetry: Boolean(afterRetryErrorNode),
  hasContentAfterRetry: Boolean(afterRetryContentNode),
  errorNodesAfterDoubleFail,
  root3ChildCount, root3StateText,
  screenTextOnBadJson,
}));
"""


@pytest.fixture(scope="module")
def node():
    found = shutil.which("node")
    if not found:
        pytest.skip("node не найден в PATH — поведенческий тест guardedRender/ui.js пропущен")
    return found


def _run_script(node, script: str) -> dict:
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def task1_result(node) -> dict:
    script = _TASK1_SCRIPT % {"ui_url": json.dumps(UI_JS.resolve().as_uri())}
    return _run_script(node, script)


def test_screen_text_reads_from_body_dataset_and_falls_back_to_empty(task1_result):
    assert task1_result["screenTextKnown"] == "Тестовый текст ошибки загрузки"
    assert task1_result["screenTextUnknown"] == ""


def test_screen_text_on_malformed_json_returns_empty_without_raising(task1_result):
    assert task1_result["screenTextOnBadJson"] == ""


def test_error_state_retry_button_label_comes_from_registry(task1_result):
    assert task1_result["retryBtnLabel"] == "Тестовая кнопка повтора"


def test_is_core_handled_error_matrix(task1_result):
    cases = task1_result["coreCases"]
    assert cases["c401"] is True
    assert cases["c403"] is True
    assert cases["cOffMini"] is True
    assert cases["cDbLocked"] is False
    assert cases["cTypeError"] is False
    assert cases["cNone"] is False


def test_guarded_render_shows_error_state_on_failure_then_retry_shows_content(task1_result):
    assert task1_result["afterFirstChildCount"] == 1
    assert task1_result["hasErrorAfterFirst"] is True
    assert task1_result["hasContentAfterRetry"] is True
    assert task1_result["hasErrorAfterRetry"] is False
    assert task1_result["afterRetryChildCount"] == 1  # повтор не дописывает второй экран


def test_guarded_render_repeated_failure_does_not_stack_error_states(task1_result):
    assert task1_result["errorNodesAfterDoubleFail"] == 1


def test_guarded_render_does_not_touch_root_when_core_already_painted_it(task1_result):
    assert task1_result["root3ChildCount"] == 1
    assert task1_result["root3StateText"] == "state-painted-by-core"
