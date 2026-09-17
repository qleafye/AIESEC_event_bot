"""Живая проверка 16.09 (п.1 продолжение) / уточнение владельца 17.09, п.2 — автоподсказки
lookup-полей (`form_types.js::lookupControl`, город/ВУЗ) рисовались ПОСЛЕ подсказки и чипов
частых значений, из-за чего на практике оказывались под экранной клавиатурой (делегат листал
экран, чтобы увидеть результат поиска).

Фикс:
- `resultsBox` (`.lookup-results`, `app.css`) — узел СРАЗУ после поля поиска, до `hint`/`chipsBox`
  (порядок `children` в `lookupControl`), сама скрывается классом `.hidden`, когда показывать
  нечего;
- строки результатов — `.lookup-row` (не общий `.row`, который разъезжался: `justify-content:
  space-between` с двумя детьми икона+текст толкал текст к правому краю);
- совпавшая часть подписи подсвечивается `<mark>` (`highlightQuery`, тот же приём, что
  `form.js::highlightMatch`, без сборки разметки строкой — T-30-08);
- выбор строки скрывает список и снимает фокус с поля (клавиатура убирается).

Харнесс — тот же приём, что `tests/test_miniapp_form_types_js_260912.py` (свой фейковый DOM
без jsdom, второй общий хелпер в проекте не заводим), + фейковый `fetch`, которого у соседних
lookup-тестов нет (там `fetch` намеренно падает — здесь он ОБЯЗАН вернуть результат, иначе
`renderResults`/`toggleResults` не проверить)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import MINIAPP_STATIC

ROOT = Path(__file__).resolve().parent.parent
FORM_TYPES_JS = MINIAPP_STATIC / "js" / "form_types.js"

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

const scrollCalls = [];

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
    // Настоящий `Node.textContent` рекурсивно спускается по ВСЕМ потомкам, не только прямым
    // текстовым узлам — нужно для проверки подсветки (`<mark>` внутри `<span>` рядом с обычным
    // текстом, `highlightQuery` в form_types.js). Соседний фейковый DOM
    // (`tests/test_miniapp_form_types_js_260912.py`) считает только прямых детей — там это
    // никогда не проверялось через вложенный элемент, здесь обязано быть точным.
    let out = "";
    for (const c of this.children) out += c.nodeType === 3 ? c.textContent : c.textContent || "";
    return out;
  }
  set textContent(v) { this.children = [new FakeText(v)]; }
  get value() { return this._value === undefined ? "" : this._value; }
  set value(v) { this._value = v; }
  // Настоящий DOM переводит фокус И вызывает подписчиков "focus"/"blur" одним действием —
  // фейк повторяет оба эффекта, иначе toggleResults() (form_types.js) не увидит
  // document.activeElement === searchInput.
  focus() { document.activeElement = this; this.dispatch("focus", {}); }
  blur() { if (document.activeElement === this) document.activeElement = null; this.dispatch("blur", {}); }
  scrollIntoView(opts) { scrollCalls.push(opts); }
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
}

globalThis.document = {
  documentElement: { dataset: {} },
  activeElement: null,
  createElement(tag) { return new FakeElement(tag); },
  createElementNS(ns, tag) { return new FakeElement(tag); },
  createTextNode(text) { return new FakeText(text); },
};
globalThis.window = globalThis;

// Фейковый транспорт (T-30-06: `q` НИКОГДА не логируется — здесь просто читается из URL, не
// логом). Без него `api.js::api()` бросает ApiError на относительном `fetch()` (см. соседний
// tests/test_miniapp_form_types_js_260912.py — там это намеренно, тут наоборот нужен ответ).
globalThis.fetch = async (url) => {
  const q = decodeURIComponent(String(url).split("q=")[1] || "");
  const payload = q
    ? { other_allowed: false, chips: ["Москва"], results: [
        { canonical: "Санкт-Петербург" }, { canonical: "Санкт-Петербург (область)" },
      ] }
    : { other_allowed: false, chips: ["Москва"], results: [] };
  return {
    ok: true,
    status: 200,
    headers: { get: (name) => (name === "Content-Type" ? "application/json" : null) },
    json: async () => payload,
  };
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

const spec = {
  key: "city", degraded_kind: "lookup",
  v2_texts: { hint_default: "Начни печатать город" },
  lookup: { chips_enabled: true, search_enabled: true },
};
const onChangeCalls = [];
const result = m.buildV2Control(h, spec, null, (v, opts) => onChangeCalls.push({ v, opts }), {});
await sleep(30);   // первичный fetchSuggest("") — только чипы, запрос пуст, результатов нет

const control = result.control;
// `findByTag` — BFS по всему дереву: `ownInput` («свой вариант») тоже `<input>` и лежит ВЫШЕ
// по очереди обхода (прямой ребёнок control, как и .pick), чем настоящее поле поиска (на
// уровень глубже, внутри .pick/.cv) — берём его через сам `.pick`, не гадаем по тегу дерева
// целиком.
const searchInputEl = findByTag(findAll(control, "pick")[0], "input");

// 1) Порядок ВЕРХНИХ узлов control: поле -> результаты -> подсказка -> чипы -> "свой вариант".
const topOrder = control.children.map((c) => {
  if (c.classList.contains("pick")) return "search";
  if (c.classList.contains("lookup-results")) return "results";
  if (c.classList.contains("label-role")) return "hint";
  if (c.classList.contains("chips")) return "chips";
  return "other";
}).filter((x) => x !== "other");

const resultsBoxInitial = findAll(control, "lookup-results")[0];
const hiddenBeforeTyping = resultsBoxInitial.classList.contains("hidden");

// 2) Ввод запроса — результаты появляются СРАЗУ под полем, подсветка совпадения, скролл в
//    зону видимости (не перекрыта клавиатурой) сработал, пока поле в фокусе.
searchInputEl.value = "петерб";
searchInputEl.focus();
searchInputEl.dispatch("input", {});
await sleep(300);   // дебаунс 250мс + разрешение фейкового fetch

const resultsBoxAfterTyping = findAll(control, "lookup-results")[0];
const visibleAfterTyping = !resultsBoxAfterTyping.classList.contains("hidden");
const rows = findAll(control, "lookup-row");
const rowCount = rows.length;
const firstRowSpan = rows[0] ? findByTag(rows[0], "SPAN") : null;
const firstRowText = firstRowSpan ? firstRowSpan.textContent : null;
const firstRowMark = rows[0] ? findByTag(rows[0], "MARK") : null;
const firstRowMarkText = firstRowMark ? firstRowMark.textContent : null;
const scrolledWhileFocused = scrollCalls.length > 0;

// 3) Выбор строки — список скрывается, значение уходит в поле, клавиатура убирается (blur),
//    ответ помечен "законченным" (commit: true — тот же приём, что чип/тап по плитке).
rows[0].dispatch("click", {});
const resultsBoxAfterPick = findAll(control, "lookup-results")[0];
const hiddenAfterPick = resultsBoxAfterPick.classList.contains("hidden");
const inputValueAfterPick = searchInputEl.value;
const focusedAfterPick = document.activeElement === searchInputEl;
const lastOnChange = onChangeCalls[onChangeCalls.length - 1];

console.log(JSON.stringify({
  topOrder,
  hiddenBeforeTyping,
  visibleAfterTyping,
  rowCount,
  firstRowText,
  firstRowMarkText,
  scrolledWhileFocused,
  hiddenAfterPick,
  inputValueAfterPick,
  focusedAfterPick,
  lastOnChange,
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческие тесты выпадающего списка lookup пропущены")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_TYPES_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_results_node_sits_right_after_search_field(js_result):
    """Уточнение владельца 17.09 (п.2): результаты — СРАЗУ под полем ввода, до подсказки и
    чипов частых значений (были ниже — расходились с экранной клавиатурой)."""
    assert js_result["topOrder"] == ["search", "results", "hint", "chips"]


def test_results_box_hidden_before_typing(js_result):
    assert js_result["hiddenBeforeTyping"] is True


def test_results_appear_with_highlighted_match_while_typing(js_result):
    assert js_result["visibleAfterTyping"] is True
    assert js_result["rowCount"] == 2
    assert js_result["firstRowText"] == "Санкт-Петербург"
    assert js_result["firstRowMarkText"] == "Петерб"


def test_field_scrolled_into_view_while_focused(js_result):
    assert js_result["scrolledWhileFocused"] is True


def test_pick_hides_list_fills_field_and_blurs(js_result):
    assert js_result["hiddenAfterPick"] is True
    assert js_result["inputValueAfterPick"] == "Санкт-Петербург"
    assert js_result["focusedAfterPick"] is False
    assert js_result["lastOnChange"] == {"v": "Санкт-Петербург", "opts": {"commit": True}}
