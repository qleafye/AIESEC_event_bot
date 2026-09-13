"""Phase 30 (30-03, задача 5, A2-01..A2-06): сторож нового модуля `form_types.js`.

Форма — та же, что `tests/test_miniapp_form_controls_js_260911.py`: структурные проверки
работают без `node` (кириллица/`innerHTML`/цветовые литералы — регэксп по исходнику),
поведенческие — node-подпроцесс без jsdom (свой фейковый DOM-пролог, второй общий хелпер в
проекте не заводим — та же дисциплина дублирования, что у соседних JS-тестов). Без `node` в
PATH поведенческие тесты пропускаются, структурные — исполняются всегда.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import (
    MINIAPP_STATIC,
    _HEX_OR_RGB_COLOR,
    _STRING_LITERAL,
    _js_without_comments,
)

ROOT = Path(__file__).resolve().parent.parent
FORM_TYPES_JS = MINIAPP_STATIC / "js" / "form_types.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ── структурные проверки (без node) ─────────────────────────────────────────────────────────

def test_no_cyrillic_string_literals_outside_comments():
    text = _js_without_comments(FORM_TYPES_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)


def test_no_innerhtml():
    text = _js_without_comments(FORM_TYPES_JS)
    assert "innerHTML" not in text


def test_no_hardcoded_colors():
    text = _js_without_comments(FORM_TYPES_JS)
    assert not _HEX_OR_RGB_COLOR.search(text)


def test_five_literal_kind_branches_present():
    text = _js_without_comments(FORM_TYPES_JS)
    for kind in ("select", "lookup", "multi", "link", "text"):
        assert f'case "{kind}"' in text, kind


def test_module_lives_in_js_root_not_screens():
    """`tests/test_miniapp_frontend.py::test_every_screen_module_is_registered_in_routes`
    требует маршрут для каждого файла `js/screens/*.js` — `form_types.js` не экран, у него нет
    маршрута, поэтому обязан жить в корне `js/`, не в `js/screens/`."""
    assert FORM_TYPES_JS.is_file()
    assert FORM_TYPES_JS.parent.name == "js"


def test_export_buildV2Control_present():
    text = _js_without_comments(FORM_TYPES_JS)
    assert "export function buildV2Control" in text


# ── поведенческие проверки (node-подпроцесс без jsdom) ──────────────────────────────────────

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
// lookupControl делает динамический import("./api.js") — модульный верх api.js читает
// window.Telegram немедленно; без globalThis.window это ReferenceError (не ошибка теста,
// см. докстринг form_types.js про статический/динамический импорт). `fetch()` дальше всё
// равно упадёт на относительном пути без базового URL — лов в try/catch внутри fetchSuggest,
// это ОЖИДАЕМО и не мешает структурным DOM-проверкам (chips/search/own-option отключаются
// флагами ДО похода в сеть).
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

function findByTag(root, tag) {
  const upper = tag.toUpperCase();
  const stack = [root];
  while (stack.length) {
    const node = stack.shift();
    if (!node || node.nodeType === 3) continue;
    if (node.tagName === upper) return node;
    if (node.children) stack.push(...node.children);
  }
  return null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
"""

NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);

// 1) select: плитка ставит aria-checked при выборе.
const selectSpec = {
  key: "alumni_status", degraded_kind: "select",
  options: ["Аламни", "Айсекер"], option_labels: {}, v2_texts: {},
};
const selectCalls = [];
const selectResult = m.buildV2Control(h, selectSpec, null, (v) => selectCalls.push(v), {});
const tiles = findAll(selectResult.control, "opt-tile");
const beforeChecked = tiles.map((t) => t.getAttribute("aria-checked"));
tiles[0].dispatch("click", {});
const afterChecked = tiles.map((t) => t.getAttribute("aria-checked"));

// 2) multi: нельзя выбрать больше max_select.
const multiSpec = {
  key: "stack", degraded_kind: "multi", max_select: 1,
  options: ["Python", "Go"], option_labels: {}, v2_texts: {},
};
const multiCalls = [];
const multiResult = m.buildV2Control(h, multiSpec, [], (v) => multiCalls.push(v.slice()), { chips: true });
const chips = findAll(multiResult.control, "chip-pick");
chips[0].dispatch("click", {});
chips[1].dispatch("click", {});   // лимит 1 — второй тап не должен добавиться
const afterMultiCalls = multiCalls.length ? multiCalls[multiCalls.length - 1] : [];

// 3) lookup без chips — ряда чипов нет.
const lookupNoChipsSpec = { key: "university", degraded_kind: "lookup", v2_texts: {} };
const lookupNoChipsResult = m.buildV2Control(h, lookupNoChipsSpec, null, () => {}, { chips: false, lookup_search: true });
await sleep(30);
const lookupNoChipsHasChips = findAll(lookupNoChipsResult.control, "chip-pick").length > 0;

// 4) lookup без lookup_search — поля поиска нет (проверяем по отсутствию строки .pick с полем).
const lookupNoSearchSpec = { key: "city", degraded_kind: "lookup", v2_texts: {} };
const lookupNoSearchResult = m.buildV2Control(h, lookupNoSearchSpec, null, () => {}, { chips: true, lookup_search: false });
await sleep(30);
const lookupNoSearchHasSearchIcon = findAll(lookupNoSearchResult.control, "pick").length > 0;

// 5) lookup: свой вариант выключен (other_allowed=false по умолчанию — сеть недоступна в
// этом окружении, other_allowed остаётся false) — ghost-чипа нет.
const lookupOwnOffResult = lookupNoChipsResult;
const lookupOwnOffHasGhost = findAll(lookupOwnOffResult.control, "ghost").length > 0;

// 6) link: валидная ссылка получает класс состояния "распознано" (.field.ok).
const linkSpec = {
  key: "resume_link", type: "url", degraded_kind: "link", required: false, v2_texts: {},
};
const linkResult = m.buildV2Control(h, linkSpec, null, () => {}, {});
const linkInput = findByTag(linkResult.control, "input");
linkInput.value = "https://vk.com/ivanova_maria";
linkInput.dispatch("input", {});
const linkFieldOk = findAll(linkResult.control, "ok").length > 0;

// 7) footer: каждый v2-контрол либо не рисует собственный .btn (используется футер экрана),
// либо, если рисует, это .btn-класс — headless-съёмка видит только .btn, не MainButton.
const anyBtnClassLeak = findAll(selectResult.control, "btn").length +
  findAll(multiResult.control, "btn").length +
  findAll(linkResult.control, "btn").length;

console.log(JSON.stringify({
  beforeChecked, afterChecked,
  afterMultiCalls,
  lookupNoChipsHasChips,
  lookupNoSearchHasSearchIcon,
  lookupOwnOffHasGhost,
  linkFieldOk,
  anyBtnClassLeak,
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческие тесты form_types.js пропущены")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_TYPES_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_select_tile_sets_aria_checked(js_result):
    assert js_result["beforeChecked"] == ["false", "false"]
    assert js_result["afterChecked"] == ["true", "false"]


def test_multi_does_not_exceed_max_select(js_result):
    assert js_result["afterMultiCalls"] == ["Python"]


def test_lookup_without_chips_renders_no_chips(js_result):
    assert js_result["lookupNoChipsHasChips"] is False


def test_lookup_without_search_renders_no_search_field(js_result):
    assert js_result["lookupNoSearchHasSearchIcon"] is False


def test_lookup_own_option_off_has_no_ghost_chip(js_result):
    assert js_result["lookupOwnOffHasGhost"] is False


def test_link_recognized_state_on_valid_url(js_result):
    assert js_result["linkFieldOk"] is True


def test_footer_button_uses_btn_class_not_custom_one(js_result):
    # `.btn` в футере рисует screens/form.js (план 30-03 задача 4), не сами v2-контролы —
    # ноль совпадений здесь ожидаемо и означает отсутствие второй, самодельной кнопки.
    assert js_result["anyBtnClassLeak"] == 0
