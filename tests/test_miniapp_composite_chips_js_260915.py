"""Квик 260915-skg, Задача 2 (P1b, решение владельца «две плитки»): тумблер карточки
«Образование» пишет СТРОКУ `education_status`, а не bool. Тумблер ВЫКЛ рисует две плитки
статуса (`spec.composite.not_studying_options`) под тумблером вместо булева.

Форма — та же, что `tests/test_miniapp_form_types_js_260912.py`: структурные проверки без
`node` (кириллица/`innerHTML` — regex по исходнику), поведенческие — node-подпроцесс без
jsdom. Без `node` в PATH поведенческие тесты пропускаются, структурные исполняются всегда.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import MINIAPP_STATIC, _STRING_LITERAL, _js_without_comments

ROOT = Path(__file__).resolve().parent.parent
FORM_TYPES_JS = MINIAPP_STATIC / "js" / "form_types.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ── структурные проверки (без node, сторожа form_types.js остаются зелёными) ───────────────

def test_no_cyrillic_string_literals_outside_comments():
    text = _js_without_comments(FORM_TYPES_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)


def test_no_innerhtml():
    text = _js_without_comments(FORM_TYPES_JS)
    assert "innerHTML" not in text


def test_composite_card_reads_studying_option_and_not_studying_options_from_spec():
    text = _js_without_comments(FORM_TYPES_JS)
    assert "studying_option" in text
    assert "not_studying_options" in text


def test_composite_card_never_assigns_boolean_studying_to_state():
    """Регресс known stub 30-04: `state[togglePart.key] = studying` (bool) больше не встречается
    — тумблер обязан писать `studyingOption`/выбранную плитку, не булево значение."""
    text = _js_without_comments(FORM_TYPES_JS)
    assert "state[togglePart.key] = studying" not in text
    assert "state[toggleKey] = studyingOption" in text


# ── поведенческие проверки (node-подпроцесс без jsdom, тот же фейковый DOM-пролог) ──────────

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
  animate() { return { finished: Promise.resolve() }; }
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
  querySelectorAll(sel) {
    const cls = sel.startsWith(".") ? sel.slice(1) : null;
    const out = [];
    const stack = [...this.children];
    while (stack.length) {
      const node = stack.shift();
      if (!node || node.nodeType === 3) continue;
      if (cls && node.classList && node.classList.contains(cls)) out.push(node);
      if (node.children) stack.push(...node.children);
    }
    return out;
  }
}

globalThis.document = {
  createElement(tag) { return new FakeElement(tag); },
  createElementNS(ns, tag) { return new FakeElement(tag); },
  createTextNode(text) { return new FakeText(text); },
};
globalThis.window = globalThis;

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

function findAll(root, cls) {
  const out = [];
  const stack = [root];
  while (stack.length) {
    const node = stack.shift();
    if (!node || node.nodeType === 3) continue;
    if (node.classList && node.classList.contains(cls)) out.push(node);
    if (node.children) stack.push(...node.children);
  }
  return out;
}
"""

NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);

const baseTexts = {
  toggle_on_label: "on", toggle_off_label: "off", toggle_on_hint: "h1", toggle_off_hint: "h2",
};

// 1) тумблер ВКЛ по умолчанию — карточка не блокирует кнопку (studying_option непуст,
// state[toggleKey] уже строка при первой отрисовке, до любого onChange).
const onSpec = {
  key: "education_status", degraded_kind: "composite", v2_texts: baseTexts,
  composite: {
    group: "education", toggle_step: "education_status",
    studying_option: "Да, очно", not_studying_options: ["Завершил", "Не получал"],
    parts: [{ key: "education_status", label: "Образование" }],
  },
};
const onResult = m.buildV2Control(h, onSpec, null, () => {}, {});
const onDisabledInitially = onResult.disabled;

// 2) переключение ВЫКЛ -> две плитки статуса появляются, ничего не выбрано -> кнопка заблокирована.
const offCalls = [];
const offFooterCalls = [];
const offResult = m.buildV2Control(h, onSpec, null, (v) => offCalls.push({ ...v }), {});
offResult.onFooterChange((label, disabled) => offFooterCalls.push(disabled));
const toggleRow = findAll(offResult.control, "swrow")[0];
toggleRow.dispatch("click", {});
const statusChipsAfterOff = findAll(offResult.control, "chip-pick");
const disabledAfterToggleOff = offFooterCalls[offFooterCalls.length - 1];
const stateAfterToggleOffValue = offCalls.length ? offCalls[offCalls.length - 1].education_status : "unset";

// 3) клик по плитке статуса -> onChange отдаёт подпись плитки, кнопка разблокирована, on ровно
// на одной плитке.
statusChipsAfterOff[0].dispatch("click", {});
const chipClickValue = offCalls[offCalls.length - 1].education_status;
const disabledAfterChipPick = offFooterCalls[offFooterCalls.length - 1];
const onClassCount = statusChipsAfterOff.filter((c) => c.classList.contains("on")).length;

// 4) обратное переключение ВЫКЛ -> ВКЛ возвращает studying_option и снимает ошибку.
toggleRow.dispatch("click", {});
const backToOnValue = offCalls[offCalls.length - 1].education_status;
const disabledAfterBackToOn = offFooterCalls[offFooterCalls.length - 1];

// 5) прежний ответ «не учится» предвыбирает плитку при первой отрисовке (без клика).
const prefillSpec = {
  key: "education_status", degraded_kind: "composite", v2_texts: baseTexts,
  composite: {
    group: "education", toggle_step: "education_status", studying: false,
    studying_option: "Да, очно", not_studying_options: ["Завершил", "Не получал"],
    parts: [{ key: "education_status", label: "Образование", value: "Завершил" }],
  },
};
const prefillResult = m.buildV2Control(h, prefillSpec, null, () => {}, {});
const prefillChips = findAll(prefillResult.control, "chip-pick");
const prefillOnChip = prefillChips.find((c) => c.classList.contains("on"));
const prefillDisabled = prefillResult.disabled;

console.log(JSON.stringify({
  onDisabledInitially,
  statusChipsAfterOffCount: statusChipsAfterOff.length,
  disabledAfterToggleOff, stateAfterToggleOffValue,
  chipClickValue, disabledAfterChipPick, onClassCount,
  backToOnValue, disabledAfterBackToOn,
  prefillHasOnChip: Boolean(prefillOnChip),
  prefillDisabled,
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческие тесты пропущены")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_TYPES_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_toggle_on_by_default_does_not_block_footer(js_result):
    assert js_result["onDisabledInitially"] is False


def test_toggle_off_renders_two_status_chips_and_blocks_footer(js_result):
    assert js_result["statusChipsAfterOffCount"] == 2
    assert js_result["disabledAfterToggleOff"] is True
    assert js_result["stateAfterToggleOffValue"] == ""


def test_chip_click_sends_label_and_unblocks_footer(js_result):
    assert js_result["chipClickValue"] == "Завершил"
    assert js_result["disabledAfterChipPick"] is False
    assert js_result["onClassCount"] == 1


def test_toggle_back_on_restores_studying_option(js_result):
    assert js_result["backToOnValue"] == "Да, очно"
    assert js_result["disabledAfterBackToOn"] is False


def test_prior_not_studying_answer_preselects_chip_on_first_render(js_result):
    assert js_result["prefillHasOnChip"] is True
    assert js_result["prefillDisabled"] is False
