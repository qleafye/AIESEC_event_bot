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


def test_seven_literal_kind_branches_present():
    """Было пять (30-03) — план 30-04 (задача 3) добавил composite/repeatable, снимая их из
    `PENDING_PROJECTIONS` со стороны Mini App (чат остаётся под заглушкой до плана 30-06)."""
    text = _js_without_comments(FORM_TYPES_JS)
    for kind in ("select", "lookup", "multi", "link", "text", "composite", "repeatable"):
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

// 2b) Приёмка 17.09 (находка 2): `min_select` — обязательный multi (сервер отвергает пустой
// список, "Форматы форума" и любой сегодняшний multi-шаг) НЕ предлагает «Пропустить» при нуле
// выбранных — кнопка дизейблена подписью `pick_min`, а не рабочей на вид `skip_button`.
const requiredMultiSpec = {
  key: "formats", degraded_kind: "multi", min_select: 1,
  options: ["Онлайн", "Офлайн"], option_labels: {},
  v2_texts: { skip_button: "Пропустить", continue_button: "Продолжить", pick_min: "Выбери минимум {min}" },
};
const requiredMultiFooterCalls = [];
const requiredMultiResult = m.buildV2Control(h, requiredMultiSpec, [], () => {}, {});
requiredMultiResult.onFooterChange((label, disabled) => requiredMultiFooterCalls.push({ label, disabled }));
const requiredMultiInitial = requiredMultiFooterCalls[requiredMultiFooterCalls.length - 1];
const requiredMultiChip = findAll(requiredMultiResult.control, "chip-pick")[0];
requiredMultiChip.dispatch("click", {});
const requiredMultiAfterPick = requiredMultiFooterCalls[requiredMultiFooterCalls.length - 1];

// 2c) необязательный multi (`min_select: 0`, гипотетический будущий skip-allowed шаг) —
// прежнее поведение сохранено: ноль выбранных предлагает `skip_button`, не дизейблит кнопку.
const optionalMultiSpec = {
  key: "goal", degraded_kind: "multi", min_select: 0,
  options: ["А", "Б"], option_labels: {},
  v2_texts: { skip_button: "Пропустить", continue_button: "Продолжить", pick_min: "Выбери минимум {min}" },
};
const optionalMultiFooterCalls = [];
const optionalMultiResult = m.buildV2Control(h, optionalMultiSpec, [], () => {}, {});
optionalMultiResult.onFooterChange((label, disabled) => optionalMultiFooterCalls.push({ label, disabled }));
const optionalMultiInitial = optionalMultiFooterCalls[optionalMultiFooterCalls.length - 1];

// 3) lookup без chips — ряда чипов нет. Деградация — из `spec.lookup` (30-08 задача A:
// `reg_engine.lookup_render_flags` уже свёл глобальный тумблер с атрибутом списка, компонент
// читает готовый результат, не `flags.chips` напрямую).
const lookupNoChipsSpec = {
  key: "university", degraded_kind: "lookup", v2_texts: {},
  lookup: { chips_enabled: false, search_enabled: true },
};
const lookupNoChipsResult = m.buildV2Control(h, lookupNoChipsSpec, null, () => {}, {});
await sleep(30);
const lookupNoChipsHasChips = findAll(lookupNoChipsResult.control, "chip-pick").length > 0;

// 4) lookup без lookup_search — поля поиска нет (проверяем по отсутствию строки .pick с полем).
const lookupNoSearchSpec = {
  key: "city", degraded_kind: "lookup", v2_texts: {},
  lookup: { chips_enabled: true, search_enabled: false },
};
const lookupNoSearchResult = m.buildV2Control(h, lookupNoSearchSpec, null, () => {}, {});
await sleep(30);
const lookupNoSearchHasSearchIcon = findAll(lookupNoSearchResult.control, "pick").length > 0;

// 4b) lookup: оба атрибута списка выключены (при включённых глобальных тумблерах) —
// компонент разворачивается в голое текстовое поле сам (30-08 задача A), не остаётся
// полурабочим (ни чипов, ни поиска, ни списка результатов).
const lookupBothOffSpec = {
  key: "city", degraded_kind: "lookup", v2_texts: {}, max_len: 100,
  lookup: { chips_enabled: false, search_enabled: false },
};
const lookupBothOffResult = m.buildV2Control(h, lookupBothOffSpec, null, () => {}, {});
const lookupBothOffIsPlainInput = lookupBothOffResult.control.tagName === "INPUT";

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

// 8) composite: единственная включённая часть группы (только тумблер) — карточка НЕ
// откатывается к легаси-шагу, рисуется из того, что есть (30-CONTEXT.md реш. 2).
const compositeSingleSpec = {
  key: "education_status", degraded_kind: "composite", v2_texts: {
    toggle_on_label: "Сейчас учусь здесь", toggle_off_label: "Уже не учусь",
    toggle_on_hint: "h1", toggle_off_hint: "h2",
  },
  composite: {
    group: "education", toggle_step: "education_status",
    parts: [{ key: "education_status", label: "Образование" }],
  },
};
const compositeSingleResult = m.buildV2Control(h, compositeSingleSpec, null, () => {}, {});
const compositeSingleFieldParts = findAll(compositeSingleResult.control, "cpart")
  .filter((n) => !n.classList.contains("hidden")).length;

// 9) composite: ошибка в одной части (программа превышает max_len) не блокирует курс —
// чип курса остаётся кликабельным и переключается независимо от соседней ошибки.
const compositeFullSpec = {
  key: "education_status", degraded_kind: "composite", v2_texts: {
    toggle_on_label: "on", toggle_off_label: "off", toggle_on_hint: "h1", toggle_off_hint: "h2",
  },
  composite: {
    group: "education", toggle_step: "education_status",
    parts: [
      { key: "education_status", label: "Образование" },
      { key: "course", label: "Курс", options: ["1", "2", "3"], option_labels: {} },
      { key: "study_field", label: "Программа", required: false, max_len: 5 },
    ],
  },
};
const compositeFooterCalls = [];
const compositeFullResult = m.buildV2Control(h, compositeFullSpec, null, () => {}, {});
compositeFullResult.onFooterChange((label, disabled) => compositeFooterCalls.push(disabled));
const studyFieldInput = findByTag(compositeFullResult.control, "input");
studyFieldInput.value = "слишком длинная программа обучения";
studyFieldInput.dispatch("input", {});
const disabledAfterError = compositeFooterCalls[compositeFooterCalls.length - 1];
const courseChipsAfterError = findAll(compositeFullResult.control, "chip-pick");
courseChipsAfterError[1].dispatch("click", {});
const courseStillClickable = courseChipsAfterError[1].classList.contains("on");

// 10) composite: тумблер выключает видимость частей группы (aria-hidden).
const toggleRowNode = findAll(compositeFullResult.control, "swrow")[0];
toggleRowNode.dispatch("click", {});
const hiddenPartsAfterToggleOff = findAll(compositeFullResult.control, "cpart")
  .filter((n) => n.getAttribute("aria-hidden") === "true").length;

// 10б) composite (план 30-05, задача 0б, хвост 30-04): `emit()` отдаёт наружу ОБЪЕКТ-патч
// нескольких колонок, не скаляр — screens/form.js::goNext() узнаёт его по typeof/Array.isArray.
const compositeEmitCalls = [];
const compositeEmitResult = m.buildV2Control(h, compositeFullSpec, null, (v) => compositeEmitCalls.push(v), {});
const emitCourseChip = findAll(compositeEmitResult.control, "chip-pick")[0];
emitCourseChip.dispatch("click", {});
const lastEmitPatch = compositeEmitCalls[compositeEmitCalls.length - 1];
const emitPatchIsPlainObject = lastEmitPatch !== null && typeof lastEmitPatch === "object"
  && !Array.isArray(lastEmitPatch);
const emitPatchHasCourseKey = Object.prototype.hasOwnProperty.call(lastEmitPatch || {}, "course");

// 10в) composite: `spec.composite.studying === false` (сервер уже знает прежний ответ
// «не учится», reg_engine.form_spec) стартует карточку СО СКРЫТЫМИ частями, без клика по
// тумблеру — 30-04 раньше всегда стартовал с `studying = true` (30-04-SUMMARY.md Known Stubs).
const compositeNotStudyingSpec = {
  key: "education_status", degraded_kind: "composite", v2_texts: {
    toggle_on_label: "on", toggle_off_label: "off", toggle_on_hint: "h1", toggle_off_hint: "h2",
  },
  composite: {
    group: "education", toggle_step: "education_status", studying: false,
    parts: [
      { key: "education_status", label: "Образование" },
      { key: "course", label: "Курс", options: ["1", "2", "3"], option_labels: {} },
    ],
  },
};
const compositeNotStudyingResult = m.buildV2Control(h, compositeNotStudyingSpec, null, () => {}, {});
const hiddenPartsWithoutClick = findAll(compositeNotStudyingResult.control, "cpart")
  .filter((n) => n.getAttribute("aria-hidden") === "true").length;

// 10г) composite: прежние значения (`part.value`) из спеки предзаполняют поля/чипы карточки.
const compositeWithValuesSpec = {
  key: "education_status", degraded_kind: "composite", v2_texts: {
    toggle_on_label: "on", toggle_off_label: "off", toggle_on_hint: "h1", toggle_off_hint: "h2",
  },
  composite: {
    group: "education", toggle_step: "education_status",
    parts: [
      { key: "education_status", label: "Образование" },
      { key: "course", label: "Курс", options: ["1", "2", "3"], option_labels: {}, value: "2" },
      { key: "study_field", label: "Программа", required: false, max_len: 40, value: "Менеджмент" },
    ],
  },
};
const compositeWithValuesResult = m.buildV2Control(h, compositeWithValuesSpec, null, () => {}, {});
const prefilledCourseChip = findAll(compositeWithValuesResult.control, "chip-pick")
  .find((c) => c.classList.contains("on"));
const prefilledCourseText = prefilledCourseChip ? findByTag(prefilledCourseChip, "span").textContent : null;
const prefilledStudyFieldInput = findByTag(compositeWithValuesResult.control, "input");
const prefilledStudyFieldValue = prefilledStudyFieldInput ? prefilledStudyFieldInput.value : null;

// 11) repeatable: кнопка «+ Добавить» исчезает по достижении repeatable_max, блоков не больше.
const repeatableSpec = {
  key: "mini_portfolio", degraded_kind: "repeatable", repeatable_max: 1,
  v2_texts: { item_label: "Проект {n}", edit_action: "Изменить", add_button: "+ Добавить проект" },
};
const repeatableCalls = [];
const repeatableResult = m.buildV2Control(h, repeatableSpec, [], (v) => repeatableCalls.push(v), {});
const repeatableAddBtn = findAll(repeatableResult.control, "dash")[0];
repeatableAddBtn.dispatch("click", {});
const repeatableCardsAfterOneAdd = findAll(repeatableResult.control, "card").length;
const repeatableAddHiddenAtMax = repeatableAddBtn.classList.contains("hidden");

console.log(JSON.stringify({
  beforeChecked, afterChecked,
  afterMultiCalls,
  requiredMultiInitial, requiredMultiAfterPick,
  optionalMultiInitial,
  lookupNoChipsHasChips,
  lookupNoSearchHasSearchIcon,
  lookupBothOffIsPlainInput,
  lookupOwnOffHasGhost,
  linkFieldOk,
  anyBtnClassLeak,
  compositeSingleFieldParts,
  disabledAfterError,
  courseStillClickable,
  hiddenPartsAfterToggleOff,
  emitPatchIsPlainObject,
  emitPatchHasCourseKey,
  hiddenPartsWithoutClick,
  prefilledCourseText,
  prefilledStudyFieldValue,
  repeatableCardsAfterOneAdd,
  repeatableAddHiddenAtMax,
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


def test_required_multi_disables_button_below_min_select(js_result):
    """Приёмка 17.09 (находка 2): обязательный multi (`min_select: 1`, "Форматы форума" и
    любой сегодняшний multi-шаг) с нулём выбранных дизейблит главную кнопку с подписью
    `pick_min`, а НЕ предлагает рабочую на вид `skip_button` (сервер отвергает пустой список)."""
    initial = js_result["requiredMultiInitial"]
    assert initial["disabled"] is True
    assert initial["label"] == "Выбери минимум 1"
    after_pick = js_result["requiredMultiAfterPick"]
    assert after_pick["disabled"] is False


def test_optional_multi_keeps_skip_button_at_zero(js_result):
    """`min_select: 0` (гипотетический будущий skip-allowed multi) — прежнее поведение:
    ноль выбранных предлагает `skip_button`, кнопка НЕ дизейблена."""
    initial = js_result["optionalMultiInitial"]
    assert initial["disabled"] is False
    assert initial["label"] == "Пропустить"


def test_lookup_without_chips_renders_no_chips(js_result):
    assert js_result["lookupNoChipsHasChips"] is False


def test_lookup_without_search_renders_no_search_field(js_result):
    assert js_result["lookupNoSearchHasSearchIcon"] is False


def test_lookup_both_list_attributes_off_degrades_to_plain_input(js_result):
    """30-08 задача A: `spec.lookup` — оба атрибута списка выключены при включённых глобальных
    тумблерах (`degrade_kind()` этого сочетания не видит) — компонент разворачивается в голое
    `<input>` сам, не оставляет делегата с полурабочим списком без чипов/поиска/результатов."""
    assert js_result["lookupBothOffIsPlainInput"] is True


def test_lookup_own_option_off_has_no_ghost_chip(js_result):
    assert js_result["lookupOwnOffHasGhost"] is False


def test_link_recognized_state_on_valid_url(js_result):
    assert js_result["linkFieldOk"] is True


def test_footer_button_uses_btn_class_not_custom_one(js_result):
    # `.btn` в футере рисует screens/form.js (план 30-03 задача 4), не сами v2-контролы —
    # ноль совпадений здесь ожидаемо и означает отсутствие второй, самодельной кнопки.
    assert js_result["anyBtnClassLeak"] == 0


# ── composite/repeatable (план 30-04, задача 4, A2-04/A2-05) ───────────────────────────────

def test_composite_renders_from_single_enabled_part_without_falling_back_to_legacy(js_result):
    """30-CONTEXT.md реш. 2: карточка всегда, из включённых частей — единственная включённая
    часть (тумблер) не откатывает рендер к легаси-текстовому полю."""
    assert js_result["compositeSingleFieldParts"] >= 1


def test_composite_error_in_one_part_does_not_block_the_others(js_result):
    """30-UI-SPEC.md § «3. composite» → «Состояния»: ошибка одной части блокирует ТОЛЬКО
    главную кнопку (через `onFooterChange`), соседняя часть (курс) остаётся кликабельной."""
    assert js_result["disabledAfterError"] is True
    assert js_result["courseStillClickable"] is True


def test_composite_toggle_hides_parts_via_aria_hidden(js_result):
    """30-UI-SPEC.md § Accessibility: тумблер переключает `aria-hidden` на скрытых частях, а
    не просто визуальный `hidden` — скринридер не должен озвучивать исчезнувшие вопросы."""
    assert js_result["hiddenPartsAfterToggleOff"] >= 1


def test_repeatable_add_button_disappears_at_max_and_blocks_are_not_added(js_result):
    assert js_result["repeatableCardsAfterOneAdd"] == 1
    assert js_result["repeatableAddHiddenAtMax"] is True


# ── хвост 30-04 (план 30-05, задача 0б) ─────────────────────────────────────────────────────

def test_composite_emit_sends_object_patch_not_scalar(js_result):
    """screens/form.js::goNext() (план 30-05) узнаёт composite-патч по `typeof === "object"` —
    `emit()` обязан отдавать именно объект с ключами-колонками, не строку/массив."""
    assert js_result["emitPatchIsPlainObject"] is True
    assert js_result["emitPatchHasCourseKey"] is True


def test_composite_studying_false_starts_with_hidden_parts(js_result):
    """`spec.composite.studying` (reg_engine.form_spec, план 30-05) — прежний ответ «не учится»
    обязан скрыть ВУЗ/курс/программу сразу при первой отрисовке, без клика по тумблеру."""
    assert js_result["hiddenPartsWithoutClick"] >= 1


def test_composite_parts_prefill_from_spec_value(js_result):
    """`spec.composite.parts[*].value` (reg_engine.form_spec, план 30-05) предзаполняет поля
    карточки прежним ответом делегата — курс чипом, программу текстом."""
    assert js_result["prefilledCourseText"] == "2"
    assert js_result["prefilledStudyFieldValue"] == "Менеджмент"
