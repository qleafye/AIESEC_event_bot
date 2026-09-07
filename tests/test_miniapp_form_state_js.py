"""Quick 260904-de4 Task 2 (D9) — поведенческая проверка `createFormState` из
`miniapp/static/js/form.js`, запущенная в node (в проекте нет JS-тестраннера, тот же приём,
что `tests/test_settings_toggle_js.py`).

`createFormState` — чистая функция, фейкового DOM не требует: импортируем `form.js` в node и
прогоняем таблицу кейсов из плана (D9): `markServerDirty` помечает колонку «грязной» для кнопки
отправки, но НЕ кладёт её в `collectPatch()` (файл резюме уже уехал на сервер — повторный PATCH
текстом затёр бы его); `applyServer` всё равно принимает свежее серверное значение для такой
колонки (`keepDirty` её не защищает — она не «своя локальная» правка); `reset()` снимает оба
набора dirty.

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
const m = await import(%(url)s);

const results = {};

// 1) markServerDirty делает isDirty true, но collectPatch колонку не содержит.
{
  const state = m.createFormState(
    [{ column: "resume_text" }, { column: "full_name" }],
    { resume_text: null, full_name: "Иванова Мария" },
  );
  state.markServerDirty("resume_text");
  results.isDirtyAfterMark = state.isDirty("resume_text");
  results.patchAfterMark = state.collectPatch();
}

// 2) applyServer после markServerDirty подменяет значение, isDirty остаётся true.
{
  const state = m.createFormState(
    [{ column: "resume_text" }],
    { resume_text: null },
  );
  state.markServerDirty("resume_text");
  const touched = state.applyServer({ resume_text: "x" });
  results.valueAfterApplyServer = state.value("resume_text");
  results.touchedByApplyServer = touched;
  results.isDirtyAfterApplyServer = state.isDirty("resume_text");
}

// 3) reset() снимает и локальный dirty, и серверный.
{
  const state = m.createFormState(
    [{ column: "resume_text" }, { column: "full_name" }],
    { resume_text: null, full_name: "A" },
  );
  state.markServerDirty("resume_text");
  state.setValue("full_name", "B");
  state.reset();
  results.isDirtyResumeAfterReset = state.isDirty("resume_text");
  results.isDirtyFullNameAfterReset = state.isDirty("full_name");
  results.fullNameAfterReset = state.value("full_name");
}

// 4) Обычный local dirty (setValue) по-прежнему уходит в collectPatch — регресс не задет.
{
  const state = m.createFormState([{ column: "full_name" }], { full_name: "A" });
  state.setValue("full_name", "B");
  results.patchWithLocalDirty = state.collectPatch();
}

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест createFormState пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_mark_server_dirty_makes_dirty_but_not_patched(result):
    assert result["isDirtyAfterMark"] is True
    assert result["patchAfterMark"] == {}


def test_apply_server_after_mark_updates_value_and_stays_dirty(result):
    assert result["valueAfterApplyServer"] == "x"
    assert result["touchedByApplyServer"] == ["resume_text"]
    assert result["isDirtyAfterApplyServer"] is True


def test_reset_clears_both_local_and_server_dirty(result):
    assert result["isDirtyResumeAfterReset"] is False
    assert result["isDirtyFullNameAfterReset"] is False
    assert result["fullNameAfterReset"] == "A"


def test_local_dirty_still_flows_into_patch(result):
    assert result["patchWithLocalDirty"] == {"full_name": "B"}


# ── Phase 28 (28-03, SU-02, 28-UI-SPEC §4): multiControl — счётчик + дизейбл при лимите ─────
# multiControl строит DOM (h()/document), в отличие от createFormState выше — фейковый
# document/h(), тот же уровень фейка, что tests/test_settings_toggle_js.py (без jsdom).

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
"""

NODE_SCRIPT_MULTI = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);
const results = {};

// A) Без max_select — разметка та же, что до этой фазы: голый .choice-grid без обёртки/счётчика.
{
  const spec = { key: "formats", type: "multi", label: "Форматы", options: ["A", "B", "C"] };
  const calls = [];
  const wrap = m.field(h, spec, [], (v) => calls.push(v));
  const control = wrap._nodes.control;
  results.withoutLimit = {
    tagName: control.tagName,
    className: control.className,
    childCount: control.children.length,
  };
}

// B) С лимитом 2 — счётчик над чекбоксами, обновляется на каждый тап, дизейбл невыбранных
// по достижении лимита, снятие выбора снимает дизейбл.
{
  const spec = {
    key: "goal", type: "multi", label: "Цель", options: ["A", "B", "C"],
    max_select: 2, limit_counter_text: "Выбрано {selected} из {max}",
  };
  const calls = [];
  const wrap = m.field(h, spec, [], (v) => calls.push(v));
  const control = wrap._nodes.control;
  const counter = control.children[0];
  const box = control.children[1];
  const checkboxes = box.children.map((label) => label.children[0]);

  results.initialCounterText = counter.textContent;
  results.counterAriaLive = counter.getAttribute("aria-live");

  checkboxes[0].checked = true;
  checkboxes[0].dispatch("change", {});
  results.afterOneSelectedCounterText = counter.textContent;
  results.afterOneSelectedDisabled = checkboxes.map((cb) => Boolean(cb.disabled));

  checkboxes[1].checked = true;
  checkboxes[1].dispatch("change", {});
  results.afterTwoSelectedCounterText = counter.textContent;
  results.afterTwoSelectedDisabled = checkboxes.map((cb) => Boolean(cb.disabled));
  results.afterTwoSelectedClassNames = box.children.map((label) => label.className);

  checkboxes[0].checked = false;
  checkboxes[0].dispatch("change", {});
  results.afterDeselectDisabled = checkboxes.map((cb) => Boolean(cb.disabled));
  results.afterDeselectCounterText = counter.textContent;
  results.callsLog = calls;
}

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def result_multi() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест multiControl(лимит) пропущен")
    script = NODE_SCRIPT_MULTI % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_multi_without_max_select_unchanged(result_multi):
    """Без `max_select` — контрол остаётся голым `.choice-grid` с тремя строками, никакой
    обёртки/счётчика не появляется (существующие мультивыборы не меняются ни на байт)."""
    without_limit = result_multi["withoutLimit"]
    assert without_limit["tagName"] == "DIV"
    assert without_limit["className"] == "choice-grid"
    assert without_limit["childCount"] == 3


def test_multi_counter_updates_on_toggle(result_multi):
    """Счётчик — `aria-live="polite"`, текст обновляется на каждый тап чекбокса."""
    assert result_multi["counterAriaLive"] == "polite"
    assert result_multi["initialCounterText"] == "Выбрано 0 из 2"
    assert result_multi["afterOneSelectedCounterText"] == "Выбрано 1 из 2"
    assert result_multi["afterTwoSelectedCounterText"] == "Выбрано 2 из 2"
    assert result_multi["afterDeselectCounterText"] == "Выбрано 1 из 2"


def test_multi_control_disables_unselected_at_limit(result_multi):
    """По достижении лимита НЕвыбранные чекбоксы дизейблятся (класс + `disabled`), уже
    выбранные остаются кликабельны; снятие выбора возвращает дизейбл обратно."""
    assert result_multi["afterOneSelectedDisabled"] == [False, False, False]
    assert result_multi["afterTwoSelectedDisabled"] == [False, False, True]
    assert result_multi["afterTwoSelectedClassNames"] == ["check", "check", "check is-limit-disabled"]
    assert result_multi["afterDeselectDisabled"] == [False, False, False]
    assert result_multi["callsLog"] == [["A"], ["A", "B"], ["B"]]
