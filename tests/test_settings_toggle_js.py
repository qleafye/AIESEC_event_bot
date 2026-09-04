"""Quick 260904-8o3 Task 1 (E1/E7): поведенческая проверка `toggleControl` из
`miniapp/static/js/form.js`, запущенная в node — тот же приём фейкового DOM без jsdom, что
`tests/test_swipe_js.py` (в проекте нет JS-тестраннера).

Контракт из <behavior> плана: клик по контролу со значением "off" красит его сам (класс `on`,
`aria-pressed="true"`, trailing = `spec.texts.on`) ДО вызова `onChange`, второй клик возвращает
исходное состояние, а внешний `row.paint(v)` (откат по ответу сервера) синхронизирует
внутренний `current`, чтобы следующий клик снова считал правильное «следующее» значение.

`toggleControl` использует `flatRow`/`icon` (ui.js/icons.js) — им нужен минимальный фейковый
`document`/`h()`, реализованные ниже (тот же уровень фейка, что `test_swipe_js.py::FakeEl`,
только с classList, синхронизированным с `className`, и `querySelector` по классу).

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
  _fromString(v) {
    this._set = new Set(String(v || "").split(/\\s+/).filter(Boolean));
  }
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
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, evt) { for (const fn of (this._listeners[type] || []).slice()) fn(evt); }
  appendChild(node) { this.children.push(node); return node; }
  append(...nodes) { for (const n of nodes) if (n != null && n !== false) this.appendChild(n); }
  get textContent() {
    return this.children.filter((c) => c.nodeType === 3).map((c) => c.textContent).join("");
  }
  set textContent(v) { this.children = [new FakeText(v)]; }
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

const m = await import(%(url)s);
// on/off-enum (D-17 Task 1) — тот же путь, что реестр отдаёт для "условного вопроса об
// образовании": settingSpec() переводит его в spec.type="toggle" (см. form.js::settingSpec),
// field() строит полную обёртку — вход, которым реально пользуется screens/settings.js.
const item = {
  key: "edu_conditional", type: "enum", options: ["on", "off"], label: "test-label",
  texts: { on: "TEXT_ON", off: "TEXT_OFF" },
};
const spec = m.settingSpec(item);

function snapshot(row) {
  const trailing = row.querySelector(".flat-row-trailing");
  return {
    on: row.classList.contains("on"),
    ariaPressed: row.getAttribute("aria-pressed"),
    trailing: trailing ? trailing.textContent : null,
  };
}

const calls = [];
const wrap = m.field(h, spec, "off", (v) => calls.push(v));
const row = wrap._nodes.control;

const afterFirstClick = (() => { row.dispatch("click", {}); return snapshot(row); })();
const callsAfterFirst = [...calls];

const afterSecondClick = (() => { row.dispatch("click", {}); return snapshot(row); })();
const callsAfterSecond = [...calls];

row.paint("off"); // откат сервером (D-08 saveToggle rollback)
const afterExternalPaint = snapshot(row);

row.dispatch("click", {});
const afterClickPostRollback = snapshot(row);
const callsAfterRollbackClick = [...calls];

console.log(JSON.stringify({
  afterFirstClick, callsAfterFirst,
  afterSecondClick, callsAfterSecond,
  afterExternalPaint,
  afterClickPostRollback, callsAfterRollbackClick,
}));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест тумблера form.js пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_first_click_paints_on_before_onchange_receives_on(result):
    assert result["afterFirstClick"]["on"] is True
    assert result["afterFirstClick"]["ariaPressed"] == "true"
    assert result["afterFirstClick"]["trailing"] == "TEXT_ON"
    assert result["callsAfterFirst"] == ["on"]


def test_second_click_paints_off_before_onchange_receives_off(result):
    assert result["afterSecondClick"]["on"] is False
    assert result["afterSecondClick"]["ariaPressed"] == "false"
    assert result["afterSecondClick"]["trailing"] == "TEXT_OFF"
    assert result["callsAfterSecond"] == ["on", "off"]


def test_external_paint_rolls_back_and_resyncs_internal_current(result):
    assert result["afterExternalPaint"]["on"] is False
    assert result["afterExternalPaint"]["ariaPressed"] == "false"
    assert result["afterExternalPaint"]["trailing"] == "TEXT_OFF"


def test_click_after_external_rollback_offers_on_not_off(result):
    # Внутренний `current` синхронизирован внешним paint("off") — следующий клик должен
    # отдать "on" (переключение от off), а не "off" (застрявшее прежнее намерение).
    assert result["afterClickPostRollback"]["on"] is True
    assert result["callsAfterRollbackClick"][-1] == "on"
