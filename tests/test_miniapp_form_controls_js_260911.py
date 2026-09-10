"""Квик 260911-2kb (W1, пункт 2) — закрытый список (`type: "select"`) в Mini App показывал
выбранным первый вариант, которого делегат не выбирал (браузер подсвечивает первый `<option>`,
`change` при этом не стреляет), и получал от сервера «Выбери вариант на клавиатуре или напиши
свой» — отправить свой вариант в приложении было нечем ни на одном из трёх select-шагов.

Node-подпроцесс без jsdom — тот же приём, что `tests/test_skillup_scoring_ui_28.py`/
`tests/test_skillup_resume_fork_ui_28.py` (в проекте нет общего DOM-хелпера для тестов, каждый
файл фазы носит свою копию фейкового DOM). Без node в PATH — `pytest.skip` с явной причиной.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import FORM_JS, _STRING_LITERAL, _js_without_comments

ROOT = Path(__file__).resolve().parent.parent
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_form_js_has_no_new_human_literals():
    text = _js_without_comments(FORM_JS)
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
"""

NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);

const baseSpec = {
  key: "study_field", type: "select", label: "Направление",
  options: ["it", "law"], option_labels: { it: "IT", law: "Юриспруденция" },
  placeholder: "Не заполнено",
};

// 1) пустое значение: первый option — placeholder, select.value === "", onChange не вызывался.
const emptyCalls = [];
const wrapEmpty = m.field(h, baseSpec, null, (v) => emptyCalls.push(v));
const selectEmpty = wrapEmpty._nodes.control;
const firstOptionEmpty = selectEmpty.children[0];

// 2) непустое значение: тот же placeholder первым, select.value === переданному значению.
const wrapFilled = m.field(h, baseSpec, "law", () => {});
const selectFilled = wrapFilled._nodes.control;
const firstOptionFilled = selectFilled.children[0];

// 3) выбор placeholder'а -> наверх уходит null, не пустая строка.
const placeholderCalls = [];
const wrapPick = m.field(h, baseSpec, "law", (v) => placeholderCalls.push(v));
const selectPick = wrapPick._nodes.control;
selectPick.value = "";
selectPick.dispatch("change", {});

// 4) выбор реального варианта -> наверх уходит КОД, не подпись.
const codeCalls = [];
const wrapCode = m.field(h, baseSpec, null, (v) => codeCalls.push(v));
const selectCode = wrapCode._nodes.control;
selectCode.value = "law";
selectCode.dispatch("change", {});

// 5) other_allowed: true — чип-переключатель + скрытое поле, ввод отдаёт текст, не "Другое".
const otherSpec = { ...baseSpec, other_allowed: true };
const otherCalls = [];
const wrapOther = m.field(h, otherSpec, null, (v) => otherCalls.push(v));
const controlOther = wrapOther._nodes.control;
const otherBtn = controlOther.children[1];
const otherInput = controlOther.children[2];
const otherHiddenBefore = otherInput.classList.contains("hidden");
otherBtn.dispatch("click", {});
const otherHiddenAfter = otherInput.classList.contains("hidden");
otherInput.value = "Мой вариант";
otherInput.dispatch("input", {});

// 6) other_allowed: false/отсутствует — разметка без chip-other/field-other, три сегодняшних
// селекта (study_field без "Другое" не встречается, но experience/readiness — этот случай).
const wrapPlain = m.field(h, baseSpec, null, () => {});
const controlPlain = wrapPlain._nodes.control;

// 7) fileControl (пункт 4): пустое значение + spec.display -> «уже сохранено» (статус —
// подпись файла, кнопка удаления видна); без display и без значения — прежнее поведение.
const fileSpecDisplay = { key: "resume", type: "file", label: "Резюме", display: "resume.pdf" };
const wrapFileDisplay = m.field(h, fileSpecDisplay, null, () => {});
const controlFileDisplay = wrapFileDisplay._nodes.control;
const statusFileDisplay = controlFileDisplay.querySelector(".dropzone-status");
const removeFileDisplay = controlFileDisplay.querySelector(".dropzone-remove");

const fileSpecEmpty = { key: "resume", type: "file", label: "Резюме" };
const wrapFileEmpty = m.field(h, fileSpecEmpty, null, () => {});
const controlFileEmpty = wrapFileEmpty._nodes.control;
const statusFileEmpty = controlFileEmpty.querySelector(".dropzone-status");
const removeFileEmpty = controlFileEmpty.querySelector(".dropzone-remove");

console.log(JSON.stringify({
  firstOptionEmptyValue: firstOptionEmpty.getAttribute("value"),
  firstOptionEmptyText: firstOptionEmpty.textContent,
  selectEmptyValue: selectEmpty.value,
  emptyCallsCount: emptyCalls.length,
  firstOptionFilledValue: firstOptionFilled.getAttribute("value"),
  selectFilledValue: selectFilled.value,
  placeholderCalls,
  codeCalls,
  otherControlTag: controlOther.tagName,
  otherControlClass: controlOther.className,
  otherBtnClass: otherBtn.className,
  otherHiddenBefore,
  otherHiddenAfter,
  otherCalls,
  plainControlTag: controlPlain.tagName,
  plainHasOtherBtn: Boolean(controlPlain.querySelector(".chip-other")),
  plainHasOtherInput: Boolean(controlPlain.querySelector(".field-other")),
  fileDisplayStatus: statusFileDisplay.textContent,
  fileDisplayRemoveHidden: removeFileDisplay.classList.contains("hidden"),
  fileEmptyStatus: statusFileEmpty.textContent,
  fileEmptyRemoveHidden: removeFileEmpty.classList.contains("hidden"),
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест selectControl form.js пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_empty_value_first_option_is_placeholder_and_no_change_fired(js_result):
    assert js_result["firstOptionEmptyValue"] == ""
    assert js_result["firstOptionEmptyText"] == "Не заполнено"
    assert js_result["selectEmptyValue"] == ""
    assert js_result["emptyCallsCount"] == 0


def test_filled_value_keeps_placeholder_first_and_select_value_set(js_result):
    assert js_result["firstOptionFilledValue"] == ""
    assert js_result["selectFilledValue"] == "law"


def test_picking_placeholder_sends_null_not_empty_string(js_result):
    assert js_result["placeholderCalls"] == [None]


def test_picking_real_option_sends_code_not_label(js_result):
    assert js_result["codeCalls"] == ["law"]


def test_other_allowed_true_shows_chip_and_hidden_field(js_result):
    assert js_result["otherControlTag"] == "DIV"
    assert "select-with-other" in js_result["otherControlClass"]
    assert "chip-other" in js_result["otherBtnClass"]
    assert js_result["otherHiddenBefore"] is True
    assert js_result["otherHiddenAfter"] is False
    assert js_result["otherCalls"] == ["Мой вариант"]


def test_other_allowed_false_has_no_other_markup(js_result):
    assert js_result["plainControlTag"] == "SELECT"
    assert js_result["plainHasOtherBtn"] is False
    assert js_result["plainHasOtherInput"] is False


def test_file_control_with_display_shows_stored_status_and_remove_button(js_result):
    assert js_result["fileDisplayStatus"] == "resume.pdf"
    assert js_result["fileDisplayRemoveHidden"] is False


def test_file_control_without_display_or_value_stays_empty(js_result):
    assert js_result["fileEmptyStatus"] == ""
    assert js_result["fileEmptyRemoveHidden"] is True
