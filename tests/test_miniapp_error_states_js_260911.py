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
SCREENS_DIR = MINIAPP_JS / "screens"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ── задача 2: пять экранов ловят отказ первичной загрузки ───────────────────────────────

@pytest.mark.parametrize("name", ["tasks.js", "card.js", "coins.js", "leaderboard.js", "faq.js"])
def test_screen_has_no_local_is_auth_error_copy(name):
    """Своя копия `isAuthError`/`errorText` снесена — только общая `isCoreHandledError`
    из `ui.js` (faq.js её носила, `card.js`/`coins.js`/`leaderboard.js`/`tasks.js` никогда
    не имели своей — сторож на будущее, чтобы седьмой не завёлся)."""
    text = _js_without_comments(SCREENS_DIR / name)
    assert "function isAuthError" not in text, name


def test_faq_js_no_longer_has_the_removed_literal_error_text():
    text = _js_without_comments(SCREENS_DIR / "faq.js")
    assert "Не удалось загрузить список — попробуйте ещё раз." not in text


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
  appendChild(node) { if (node) node._parent = this; this.children.push(node); return node; }
  append(...nodes) { for (const n of nodes) if (n != null && n !== false) this.appendChild(n); }
  replaceChildren(...nodes) {
    for (const c of this.children) if (c) c._parent = null;
    this.children = [];
    this.append(...nodes);
  }
  remove() {
    // Квик 260915-4mw: guardedRender снимает скелетон-плейсхолдер в finally — узел к этому
    // моменту может быть уже откреплён чужим replaceChildren() (leaderboard.js/submit.js,
    // gotcha 7 плана), remove() на уже откреплённом узле обязан быть безопасным no-op.
    if (this._parent) {
      this._parent.children = this._parent.children.filter((c) => c !== this);
      this._parent = null;
    }
  }
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


# ── задача 2: пять экранов через guardedRender ───────────────────────────────────────────
#
# Общий сценарий на экран: (1) первый api()-вызов первичной загрузки бросает ApiError(500) ->
# root показывает ровно состояние ошибки; клик «Повторить» с уже работающим api() рисует
# нормальный экран, состояния ошибки больше нет; (2) 403 — «ядро» красит root СИНХРОННО
# внутри api() (тот же порядок, что showState делает в api.js:53-62 до throw) — guardedRender
# не трогает root повторно, чужой error-state не дописывается поверх. tasks.js/coins.js
# дополнительно: отказ на «Показать ещё» тоже даёт состояние ошибки (сегодня — необработанный
# rejected promise после завершения render).

_TASKS_SCRIPT = _FAKE_DOM_PRELUDE + """
const mod = await import(%(url)s);

const root1 = document.createElement("div");
let calls1 = 0;
async function api1(path) {
  if (path.startsWith("/hub")) return {};
  if (path.startsWith("/tasks?")) {
    calls1++;
    if (calls1 === 1) throw err(500, "server_error");
    return { items: [{ id: 1, title: "Задание", category: "cat", category_label: "Категория",
      coins: 5, deadline_at: null, deadline_short: "скоро", overdue: false, status: "new" }],
      total: 1, empty_text: null };
  }
  throw new Error("unexpected " + path);
}
await mod.render(root1, {}, { h, api: api1, navigate: () => {}, me: {} });
const errorAfterFirst = Boolean(root1.querySelector(".error-state"));
const retryBtn = root1.querySelector(".error-state").querySelector(".btn");
await retryBtn._listeners.click[0]();
const hasContentAfterRetry = Boolean(root1.querySelector(".flat-row"));
const errorAfterRetry = Boolean(root1.querySelector(".error-state"));

const root2 = document.createElement("div");
async function api2(path) {
  if (path.startsWith("/hub")) return {};
  root2.replaceChildren(h("section", { class: "state", text: "core-painted" }));
  throw err(403);
}
await mod.render(root2, {}, { h, api: api2, navigate: () => {}, me: {} });
const coreChildCount = root2.children.length;
const coreText = root2.children[0] ? root2.children[0].textContent : null;

const root3 = document.createElement("div");
let calls3 = 0;
async function api3(path) {
  if (path.startsWith("/hub")) return {};
  if (path.startsWith("/tasks?")) {
    calls3++;
    if (calls3 === 1) return { items: [{ id: 1, title: "T1", category: "c", category_label: "C",
      coins: 1, deadline_at: null, deadline_short: "x", overdue: false, status: "new" }],
      total: 5, empty_text: null };
    throw err(500, "server_error");
  }
  throw new Error("unexpected " + path);
}
await mod.render(root3, {}, { h, api: api3, navigate: () => {}, me: {} });
const moreBtn = root3.children.flatMap((c) => c.children || []).find(
  (b) => b.tagName === "BUTTON" && (b.textContent || "").startsWith("Показать ещё"),
) || root3.querySelector(".list-foot").children.find((b) => b.tagName === "BUTTON");
await moreBtn._listeners.click[0]();
const showMoreFailShowsError = Boolean(root3.querySelector(".error-state"));

console.log(JSON.stringify({
  errorAfterFirst, hasContentAfterRetry, errorAfterRetry,
  coreChildCount, coreText, showMoreFailShowsError,
}));
"""

_CARD_SCRIPT = _FAKE_DOM_PRELUDE + """
const mod = await import(%(url)s);
const task = {
  id: 1, title: "Задание", category_label: "Категория", deadline_short: "скоро",
  coins: 5, deadline_left_text: null, overdue_hint: null, todo_eyebrow: "Что сделать",
  text: "Текст задания", proof_hint: "Фото", proof_eyebrow: "Нужно прислать",
  proof_note: "заметка", proof_type: "photo", status_line: "новое", review_note: "проверка",
  overdue: false, can_submit: true, photo_file_id: null,
};

const root1 = document.createElement("div");
let calls1 = 0;
async function api1(path) {
  calls1++;
  if (calls1 === 1) throw err(500, "server_error");
  return task;
}
await mod.render(root1, { id: "1" }, { h, api: api1, navigate: () => {}, setMainButton: () => {}, me: {} });
const errorAfterFirst = Boolean(root1.querySelector(".error-state"));
const retryBtn = root1.querySelector(".error-state").querySelector(".btn");
await retryBtn._listeners.click[0]();
const hasContentAfterRetry = Boolean(root1.children[0] && root1.children[0].className.includes("plate--task"));
const errorAfterRetry = Boolean(root1.querySelector(".error-state"));

const root2 = document.createElement("div");
async function api2(path) {
  root2.replaceChildren(h("section", { class: "state", text: "core-painted" }));
  throw err(403);
}
await mod.render(root2, { id: "1" }, { h, api: api2, navigate: () => {}, setMainButton: () => {}, me: {} });
const coreChildCount = root2.children.length;
const coreText = root2.children[0] ? root2.children[0].textContent : null;

console.log(JSON.stringify({ errorAfterFirst, hasContentAfterRetry, errorAfterRetry, coreChildCount, coreText }));
"""

_COINS_SCRIPT = _FAKE_DOM_PRELUDE + """
const mod = await import(%(url)s);

const root1 = document.createElement("div");
let balCalls = 0;
async function api1(path) {
  if (path.startsWith("/coins/balance")) {
    balCalls++;
    if (balCalls === 1) throw err(500, "server_error");
    return { balance: 100, rank: 2, participants: 10 };
  }
  if (path.startsWith("/hub")) return {};
  if (path.startsWith("/coins/history")) {
    return { items: [{ reason: "тест", created_at: "2026-01-01 00:00:00", delta: 5 }], total: 1 };
  }
  throw new Error("unexpected " + path);
}
await mod.render(root1, {}, { h, api: api1, navigate: () => {}, me: {} });
const errorAfterFirst = Boolean(root1.querySelector(".error-state"));
const retryBtn = root1.querySelector(".error-state").querySelector(".btn");
await retryBtn._listeners.click[0]();
const hasContentAfterRetry = Boolean(root1.querySelector(".flat-row"));
const errorAfterRetry = Boolean(root1.querySelector(".error-state"));

const root2 = document.createElement("div");
async function api2(path) {
  if (path.startsWith("/coins/balance")) {
    root2.replaceChildren(h("section", { class: "state", text: "core-painted" }));
    throw err(403);
  }
  throw new Error("unexpected " + path);
}
await mod.render(root2, {}, { h, api: api2, navigate: () => {}, me: {} });
const coreChildCount = root2.children.length;
const coreText = root2.children[0] ? root2.children[0].textContent : null;

const root3 = document.createElement("div");
let histCalls = 0;
async function api3(path) {
  if (path.startsWith("/coins/balance")) return { balance: 50, rank: null };
  if (path.startsWith("/hub")) return {};
  if (path.startsWith("/coins/history")) {
    histCalls++;
    if (histCalls === 1) return { items: [{ reason: "x", created_at: "2026-01-01 00:00:00", delta: 1 }], total: 5 };
    throw err(500, "server_error");
  }
  throw new Error("unexpected " + path);
}
await mod.render(root3, {}, { h, api: api3, navigate: () => {}, me: {} });
const moreBtn = root3.querySelector(".list-foot").children.find((b) => b.tagName === "BUTTON");
await moreBtn._listeners.click[0]();
const showMoreFailShowsError = Boolean(root3.querySelector(".error-state"));

console.log(JSON.stringify({
  errorAfterFirst, hasContentAfterRetry, errorAfterRetry,
  coreChildCount, coreText, showMoreFailShowsError,
}));
"""

_LEADERBOARD_SCRIPT = _FAKE_DOM_PRELUDE + """
const mod = await import(%(url)s);
const board = {
  me: { rank: 5, balance: 20 },
  items: [
    { rank: 1, name: "A", balance: 100 }, { rank: 2, name: "B", balance: 90 },
    { rank: 3, name: "C", balance: 80 }, { rank: 4, name: "D", balance: 70 },
    { rank: 5, name: "E", balance: 20, is_me: true },
  ],
  total: 5, empty_text: null,
};

const root1 = document.createElement("div");
let calls1 = 0;
async function api1(path) {
  if (path.startsWith("/hub")) return {};
  if (path.startsWith("/leaderboard")) {
    calls1++;
    if (calls1 === 1) throw err(500, "server_error");
    return board;
  }
  throw new Error("unexpected " + path);
}
await mod.render(root1, {}, { h, api: api1, me: {} });
const errorAfterFirst = Boolean(root1.querySelector(".error-state"));
const retryBtn = root1.querySelector(".error-state").querySelector(".btn");
await retryBtn._listeners.click[0]();
const hasContentAfterRetry = Boolean(root1.querySelector(".podium"));
const errorAfterRetry = Boolean(root1.querySelector(".error-state"));

const root2 = document.createElement("div");
async function api2(path) {
  if (path.startsWith("/hub")) return {};
  root2.replaceChildren(h("section", { class: "state", text: "core-painted" }));
  throw err(403);
}
await mod.render(root2, {}, { h, api: api2, me: {} });
const coreChildCount = root2.children.length;
const coreText = root2.children[0] ? root2.children[0].textContent : null;

console.log(JSON.stringify({ errorAfterFirst, hasContentAfterRetry, errorAfterRetry, coreChildCount, coreText }));
"""

_FAQ_SCRIPT = _FAKE_DOM_PRELUDE + """
document.body.dataset.sectionLabels = JSON.stringify({ faq: "❓ Частые вопросы" });
const mod = await import(%(url)s);

const root1 = document.createElement("div");
let calls1 = 0;
async function api1(path) {
  calls1++;
  if (calls1 === 1) throw err(500, "server_error");
  return { items: [{ id: 1, question: "Q1", answer: "A1" }], empty_text: null };
}
await mod.render(root1, {}, { h, api: api1, me: {} });
const errorAfterFirst = Boolean(root1.querySelector(".error-state"));
const retryBtn = root1.querySelector(".error-state").querySelector(".btn");
await retryBtn._listeners.click[0]();
const hasContentAfterRetry = Boolean(root1.querySelector(".flat-row"));
const errorAfterRetry = Boolean(root1.querySelector(".error-state"));

const root2 = document.createElement("div");
async function api2(path) {
  root2.replaceChildren(h("section", { class: "state", text: "core-painted" }));
  throw err(403);
}
await mod.render(root2, {}, { h, api: api2, me: {} });
const coreChildCount = root2.children.length;
const coreText = root2.children[0] ? root2.children[0].textContent : null;

console.log(JSON.stringify({ errorAfterFirst, hasContentAfterRetry, errorAfterRetry, coreChildCount, coreText }));
"""


@pytest.fixture(scope="module")
def tasks_result(node) -> dict:
    url = json.dumps((SCREENS_DIR / "tasks.js").resolve().as_uri())
    return _run_script(node, _TASKS_SCRIPT % {"url": url})


@pytest.fixture(scope="module")
def card_result(node) -> dict:
    url = json.dumps((SCREENS_DIR / "card.js").resolve().as_uri())
    return _run_script(node, _CARD_SCRIPT % {"url": url})


@pytest.fixture(scope="module")
def coins_result(node) -> dict:
    url = json.dumps((SCREENS_DIR / "coins.js").resolve().as_uri())
    return _run_script(node, _COINS_SCRIPT % {"url": url})


@pytest.fixture(scope="module")
def leaderboard_result(node) -> dict:
    url = json.dumps((SCREENS_DIR / "leaderboard.js").resolve().as_uri())
    return _run_script(node, _LEADERBOARD_SCRIPT % {"url": url})


@pytest.fixture(scope="module")
def faq_result(node) -> dict:
    url = json.dumps((SCREENS_DIR / "faq.js").resolve().as_uri())
    return _run_script(node, _FAQ_SCRIPT % {"url": url})


@pytest.mark.parametrize("fixture_name", ["tasks_result", "card_result", "coins_result", "leaderboard_result", "faq_result"])
def test_screen_primary_load_failure_shows_error_then_retry_recovers(fixture_name, request):
    result = request.getfixturevalue(fixture_name)
    assert result["errorAfterFirst"] is True
    assert result["hasContentAfterRetry"] is True
    assert result["errorAfterRetry"] is False


@pytest.mark.parametrize("fixture_name", ["tasks_result", "card_result", "coins_result", "leaderboard_result", "faq_result"])
def test_screen_403_leaves_core_painted_content_untouched(fixture_name, request):
    result = request.getfixturevalue(fixture_name)
    assert result["coreChildCount"] == 1
    assert result["coreText"] == "core-painted"


def test_tasks_show_more_failure_shows_error_state(tasks_result):
    assert tasks_result["showMoreFailShowsError"] is True


def test_coins_show_more_failure_shows_error_state(coins_result):
    assert coins_result["showMoreFailShowsError"] is True


# ── задача 3: submit.js — гейт закрытой сдачи + порядок карточки ────────────────────────

_SUBMIT_SCRIPT = _FAKE_DOM_PRELUDE + """
document.body.dataset.screenTexts = JSON.stringify({
  load_error: "ошибка", retry: "повтор",
  submit_pending: "уже на проверке", submit_approved: "уже принято",
  submit_limit: "попытки кончились",
});
const mod = await import(%(url)s);

const limits = {
  max_parts: 5, max_bytes: 1000000, photo_max_bytes: 500000, max_text: 500,
  too_large_text: "слишком большой", empty_hint: "нечего отправлять",
};
function noopMainButton() {}

async function gateCase(status) {
  const root = document.createElement("div");
  async function api(path) {
    if (path.startsWith("/tasks/")) return { id: 1, can_submit: false, status, title: "T" };
    if (path.startsWith("/uploads/limits")) return limits;
    throw new Error("unexpected " + path);
  }
  const mainButtonCalls = [];
  await mod.render(root, { id: "1" }, {
    h, api, navigate: () => {}, setMainButton: (...args) => mainButtonCalls.push(args), tg: null, me: {},
  });
  return {
    hasForm: Boolean(
      root.querySelector(".submit-actions") || root.querySelector(".parts-counter")
      || root.querySelector(".parts-list"),
    ),
    text: root.querySelector(".empty-state-text") ? root.querySelector(".empty-state-text").textContent : null,
    mainButtonCalledWithNull: mainButtonCalls.length > 0 && mainButtonCalls[0][0] === null,
  };
}

const pending = await gateCase("pending");
const approved = await gateCase("approved");
const rejectedLimit = await gateCase("rejected");

// can_submit: true -> форма рисуется, порядок карточки, «Готово» внутри карточки.
const rootForm = document.createElement("div");
async function apiForm(path) {
  if (path.startsWith("/tasks/")) return { id: 1, can_submit: true, status: "new", title: "T", proof_hint: "фото" };
  if (path.startsWith("/uploads/limits")) return limits;
  throw new Error("unexpected " + path);
}
await mod.render(rootForm, { id: "1" }, { h, api: apiForm, navigate: () => {}, setMainButton: noopMainButton, tg: null, me: {} });
const card = rootForm.querySelector(".submit-card");
const cardChildClasses = card.children.map((c) => c.className);
const actionsIdx = cardChildClasses.findIndex((c) => c.includes("submit-actions"));
const counterIdx = cardChildClasses.findIndex((c) => c.includes("parts-counter"));
const listIdx = cardChildClasses.findIndex((c) => c.includes("parts-list"));
const lastChild = card.children[card.children.length - 1];
const rootHasButtonAfterCard = rootForm.children.some((c) => c !== card && c.tagName === "BUTTON");

// первичная загрузка через guardedRender: отказ -> ошибка -> повтор рисует форму.
const rootFail = document.createElement("div");
let calls5 = 0;
async function apiFail(path) {
  if (path.startsWith("/tasks/")) {
    calls5++;
    if (calls5 === 1) throw err(500, "server_error");
    return { id: 1, can_submit: true, status: "new", title: "T", proof_hint: "фото" };
  }
  if (path.startsWith("/uploads/limits")) return limits;
  throw new Error("unexpected " + path);
}
await mod.render(rootFail, { id: "1" }, { h, api: apiFail, navigate: () => {}, setMainButton: noopMainButton, tg: null, me: {} });
const errorAfterFirst = Boolean(rootFail.querySelector(".error-state"));
const retryBtn = rootFail.querySelector(".error-state").querySelector(".btn");
await retryBtn._listeners.click[0]();
const hasFormAfterRetry = Boolean(rootFail.querySelector(".submit-card"));

// 403 — ядро уже покрасило, submit.js не дописывает свой error-state поверх.
const rootForbidden = document.createElement("div");
async function apiForbidden(path) {
  rootForbidden.replaceChildren(h("section", { class: "state", text: "core-painted" }));
  throw err(403);
}
await mod.render(rootForbidden, { id: "1" }, { h, api: apiForbidden, navigate: () => {}, setMainButton: noopMainButton, tg: null, me: {} });

console.log(JSON.stringify({
  pending, approved, rejectedLimit,
  cardOrderOk: actionsIdx >= 0 && actionsIdx < counterIdx && counterIdx < listIdx,
  lastChildIsDoneButton: lastChild.tagName === "BUTTON",
  rootHasButtonAfterCard,
  errorAfterFirst, hasFormAfterRetry,
  coreChildCount: rootForbidden.children.length,
  coreText: rootForbidden.children[0] ? rootForbidden.children[0].textContent : null,
}));
"""


@pytest.fixture(scope="module")
def submit_result(node) -> dict:
    url = json.dumps((SCREENS_DIR / "submit.js").resolve().as_uri())
    return _run_script(node, _SUBMIT_SCRIPT % {"url": url})


def test_submit_gate_pending_shows_state_text_no_form_and_clears_main_button(submit_result):
    pending = submit_result["pending"]
    assert pending["hasForm"] is False
    assert pending["text"] == "уже на проверке"
    assert pending["mainButtonCalledWithNull"] is True


def test_submit_gate_approved_shows_state_text_no_form(submit_result):
    approved = submit_result["approved"]
    assert approved["hasForm"] is False
    assert approved["text"] == "уже принято"


def test_submit_gate_rejected_limit_exhausted_shows_state_text_no_form(submit_result):
    rejected = submit_result["rejectedLimit"]
    assert rejected["hasForm"] is False
    assert rejected["text"] == "попытки кончились"


def test_submit_can_submit_true_renders_form_with_correct_card_order(submit_result):
    assert submit_result["cardOrderOk"] is True
    assert submit_result["lastChildIsDoneButton"] is True
    assert submit_result["rootHasButtonAfterCard"] is False


def test_submit_primary_load_failure_shows_error_then_retry_renders_form(submit_result):
    assert submit_result["errorAfterFirst"] is True
    assert submit_result["hasFormAfterRetry"] is True


def test_submit_403_leaves_core_painted_content_untouched(submit_result):
    assert submit_result["coreChildCount"] == 1
    assert submit_result["coreText"] == "core-painted"
