"""Квик 260912-l53 (задача 2) — «×» на дропзоне резюме должна удалять файл немедленно, и на
экране, и на сервере (симметрично `uploadResume`, D9). Два слоя проверки:

1. Поведенческий (node-подпроцесс, фейковый DOM) — `form.js::fileControl`: клик «×» ставит
   локальный флаг `cleared`, `spec.display` больше не воскрешает удалённый файл до ответа
   сервера, выбор нового файла флаг снимает.
2. Структурный (регэксп по исходнику) — `screens/form.js::removeResume`: определена, вызвана
   и из мастера (`drawStep`), и из обзора правки (`fieldRow.open`), шлёт PATCH с `clear`,
   условие вызова привязано к `spec.type === "file"` (не к «любой null»), в её теле нет ранней
   ветки «черновика нет — выходим» (в отличие от `uploadResume`, у которого такая ветка есть).

В проекте нет общего DOM-хелпера для node-тестов — фейковый DOM скопирован из
`tests/test_miniapp_form_controls_js_260911.py` (тот же приём, что и во всех node-тестах
фазы 28). Без node в PATH — `pytest.skip` с явной причиной.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import FORM_JS, SCREENS_DIR, _STRING_LITERAL, _js_without_comments

ROOT = Path(__file__).resolve().parent.parent
FORM_SCREEN_JS = SCREENS_DIR / "form.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Слой 1: form.js::fileControl в node — фейковый DOM (скопирован из образца квика 260911-2kb)
# ══════════════════════════════════════════════════════════════════════════════════════════

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
const m = await import(%(form_url)s);

// 1) spec.display + пустое значение -> подпись файла и видимая кнопка «×» (поведение W1 не
// сломано этим квиком).
const resumeSpec = { key: "resume", type: "file", label: "Резюме", display: "cv-old.pdf" };
const removeCalls = [];
const wrap = m.field(h, resumeSpec, null, (v) => removeCalls.push(v));
const control = wrap._nodes.control;
const status = control.querySelector(".dropzone-status");
const removeBtn = control.querySelector(".dropzone-remove");
const preview = control.children.find((c) => c.tagName === "IMG");
const input = control.children.find((c) => c.tagName === "INPUT");

const beforeStatus = status.textContent;
const beforeRemoveHidden = removeBtn.classList.contains("hidden");

// 2) клик «×» -> onChange(null), подпись пуста, миниатюра и сама кнопка скрыты — spec.display
// больше не воскрешает удалённый файл (сегодня, до фикса, — воскрешает).
removeBtn.dispatch("click", {});
const afterClickCalls = [...removeCalls];
const afterClickStatus = status.textContent;
const afterClickRemoveHidden = removeBtn.classList.contains("hidden");
const afterClickPreviewHidden = preview.classList.contains("hidden");

// 3) выбор нового файла после «×» -> подпись снова показывает имя (флаг очистки снимается).
input.files = [{ name: "new-resume.pdf" }];
input.dispatch("change", {});
const afterReuploadStatus = status.textContent;
const afterReuploadRemoveHidden = removeBtn.classList.contains("hidden");

// 4) сторож W1 (пункт 2, из образца): плейсхолдер закрытого списка по-прежнему отдаёт null,
// а не пустую строку — «null = удаление» не расползлось на другие контролы.
const selectSpec = {
  key: "study_field", type: "select", label: "Направление",
  options: ["it", "law"], option_labels: { it: "IT", law: "Юриспруденция" },
  placeholder: "Не заполнено",
};
const selectCalls = [];
const wrapSelect = m.field(h, selectSpec, "law", (v) => selectCalls.push(v));
const selectEl = wrapSelect._nodes.control;
selectEl.value = "";
selectEl.dispatch("change", {});

console.log(JSON.stringify({
  beforeStatus, beforeRemoveHidden,
  afterClickCalls, afterClickStatus, afterClickRemoveHidden, afterClickPreviewHidden,
  afterReuploadStatus, afterReuploadRemoveHidden,
  selectCalls,
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест fileControl form.js пропущен")
    script = NODE_SCRIPT % {"form_url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_display_with_empty_value_shows_stored_filename_and_remove_button(js_result):
    assert js_result["beforeStatus"] == "cv-old.pdf"
    assert js_result["beforeRemoveHidden"] is False


def test_click_remove_sends_null_and_stops_showing_stored_display(js_result):
    assert js_result["afterClickCalls"] == [None]
    assert js_result["afterClickStatus"] == ""
    assert js_result["afterClickRemoveHidden"] is True
    assert js_result["afterClickPreviewHidden"] is True


def test_selecting_new_file_after_remove_shows_its_name_again(js_result):
    assert js_result["afterReuploadStatus"] == "new-resume.pdf"
    assert js_result["afterReuploadRemoveHidden"] is False


def test_select_placeholder_still_sends_null_not_string(js_result):
    assert js_result["selectCalls"] == [None]


def test_form_js_has_no_new_human_literals():
    text = _js_without_comments(FORM_JS)
    for m_lit in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m_lit.group(0)), m_lit.group(0)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Слой 2: screens/form.js::removeResume — структурные сторожа по исходнику
# ══════════════════════════════════════════════════════════════════════════════════════════

def _screen_text() -> str:
    return _js_without_comments(FORM_SCREEN_JS)


def test_remove_resume_is_defined():
    text = _screen_text()
    assert "async function removeResume(el, ctx)" in text


def test_remove_resume_sends_clear_patch():
    text = _screen_text()
    start = text.index("async function removeResume(el, ctx)")
    # Следующая крупная секция после функции в исходнике — activated-подписка; тело функции
    # целиком лежит до неё (тот же приём границы, что использовал бы `grep -A`, но точнее).
    # `let onRefresh = null` — первая строка КОДА (не комментария) после removeResume, комментарии
    # уже вырезаны _js_without_comments, искать по ним нельзя.
    end = text.index("let onRefresh = null", start)
    body = text[start:end]
    assert 'clear: [ctx.stepKey]' in body
    assert "current.exists" not in body, "ранней ветки «черновика нет — выходим» быть не должно"


def test_remove_resume_called_from_both_wizard_and_overview():
    text = _screen_text()
    # Один и тот же вызов `removeResume(el, {` встречается дважды: в мастере (drawStep) и в
    # обзоре правки (fieldRow.open) — плюс определение функции, итого три вхождения подстроки
    # "removeResume(".
    assert text.count("removeResume(") == 3, "ожидались определение + два вызова"
    fieldrow_start = text.index("function fieldRow(spec)")
    drawstep_start = text.index("function drawStep()")
    fieldrow_region = text[fieldrow_start:text.index("function drawList()")]
    drawstep_region = text[drawstep_start:text.index("async function goNext()")]
    assert "removeResume(el, {" in fieldrow_region
    assert "removeResume(el, {" in drawstep_region


def test_remove_resume_call_is_gated_on_file_type_not_any_null():
    text = _screen_text()
    # Плейсхолдер закрытого списка (selectControl, W1 пункт 2) тоже отдаёт null — условие
    # вызова обязано проверять именно spec.type === "file", а не «v === null» само по себе.
    assert text.count('v === null && spec.type === "file"') == 2
