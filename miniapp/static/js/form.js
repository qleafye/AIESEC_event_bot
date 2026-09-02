// Общие формо-компоненты (план 21-04, D-22): один рендерер поля анкеты по типу + матрица
// состояний + dirty/diff + confirm-box + headless-утилиты (поиск, свёртка группы), которые
// фаза 22 переиспользует для веб-настроек без второго набора компонентов.
//
// Правило 0-хардкода (D-25): ни одного текстового литерала, обращённого к человеку. Подписи —
// только из `spec` (reg_engine.step_spec, приходит с сервера) или из параметров, которые
// вызывающий экран передаёт из реестра/ответа API (`payload.text`, `labels.*`,
// `confirmText`/`cancelText`/`text` в confirmBox). Здесь этому правилу подчиняется КАЖДАЯ
// строка — даже подпись кнопки confirmBox приходит параметром, как в screens/settings.js.
//
// `errorText`/`isAuthError` перенесены сюда из screens/task_edit.js и screens/settings.js
// (были дословными дублями) — сигнатура `isAuthError` расширена вторым необязательным
// параметром `excludeReasons`, чтобы оба экрана сохранили СВОЁ прежнее поведение (какие
// причины 403 не считаются гейтом авторизации) без копии тела функции.

import { icon } from "./icons.js";

// ── errorText/isAuthError (перенос из task_edit.js/settings.js) ─────────────────────────

export function errorText(err, fallback) {
  if (err && err.payload && err.payload.text) return err.payload.text;
  return fallback;
}

export function isAuthError(err, excludeReasons) {
  if (!err) return false;
  if (err.status === 401 || err.status === 503) return true;
  if (err.status === 403) {
    const except = excludeReasons || [];
    return !except.includes(err.reason);
  }
  return false;
}

// ── field(): рендер поля анкеты по spec.type ──────────────────────────────────────────

function textControl(h, spec, value, onChange, type, inputmode) {
  const attrs = { class: "input", type, id: `f-${spec.key}` };
  if (inputmode) attrs.inputmode = inputmode;
  if (spec.max_len) attrs.maxlength = String(spec.max_len);
  const input = h("input", attrs);
  input.value = value || "";
  let extra = null;
  if (spec.max_len) {
    extra = h("div", { class: "field-counter hidden" });
    const paint = () => {
      const len = input.value.length;
      extra.textContent = `${len}/${spec.max_len}`;
      extra.classList.toggle("hidden", len < spec.max_len * 0.9);
    };
    input.addEventListener("input", () => { onChange(input.value); paint(); });
    paint();
  } else {
    input.addEventListener("input", () => onChange(input.value));
  }
  return { control: input, extra };
}

function textareaControl(h, spec, value, onChange) {
  const attrs = { class: "input", id: `f-${spec.key}`, rows: "4" };
  if (spec.max_len) attrs.maxlength = String(spec.max_len);
  const area = h("textarea", attrs);
  area.value = value || "";
  const grow = () => { area.style.height = "auto"; area.style.height = `${area.scrollHeight}px`; };
  area.addEventListener("input", () => { onChange(area.value); grow(); });
  return { control: area };
}

function intControl(h, spec, value, onChange) {
  const input = h("input", { class: "input", type: "number", inputmode: "numeric", id: `f-${spec.key}` });
  if (value != null) input.value = String(value);
  input.addEventListener("input", () => onChange(input.value === "" ? null : Number(input.value)));
  return { control: input };
}

function dateControl(h, spec, value, onChange) {
  const input = h("input", { class: "input", type: "date", id: `f-${spec.key}` });
  if (value) input.value = value;
  input.addEventListener("change", () => onChange(input.value));
  return { control: input };
}

// Сегмент чипов (choice-chips/yesno, порог options.length<=4, A3 21-UI-SPEC.md). «Другое»
// (spec.other_allowed) — отдельный чип-переключатель без своего текста (иконка pen-line,
// aria-label из spec.label — данные, не литерал), раскрывающий текстовое поле рядом.
function choiceChips(h, spec, value, onChange) {
  const box = h("div", { class: "choice-chips", role: "group", "aria-label": spec.label });
  let current = value;
  const buttons = [];
  function paint() {
    for (const btn of buttons) {
      const on = btn.dataset.value === String(current);
      btn.classList.toggle("chosen", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }
  for (const opt of spec.options || []) {
    const btn = h("button", {
      class: "chip-choice", type: "button", text: opt, "data-value": opt,
      onClick: () => { current = opt; onChange(opt); paint(); },
    });
    buttons.push(btn);
    box.append(btn);
  }
  if (spec.other_allowed) {
    const otherInput = h("input", { class: "input field-other hidden", type: "text" });
    const otherBtn = h("button", {
      class: "chip-choice chip-other", type: "button", "aria-label": spec.label,
      onClick: () => {
        otherInput.classList.toggle("hidden");
        if (!otherInput.classList.contains("hidden")) otherInput.focus();
      },
    }, icon("pen-line"));
    otherInput.addEventListener("input", () => { current = otherInput.value; onChange(otherInput.value); });
    box.append(otherBtn, otherInput);
  }
  paint();
  return box;
}

function selectControl(h, spec, value, onChange) {
  const select = h("select", { class: "input", id: `f-${spec.key}` });
  for (const opt of spec.options || []) {
    select.append(h("option", { value: opt, text: opt }));
  }
  if (value != null) select.value = value;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function multiControl(h, spec, value, onChange) {
  const chosen = new Set(Array.isArray(value) ? value : []);
  const box = h("div", { class: "choice-grid", role: "group", "aria-label": spec.label });
  for (const opt of spec.options || []) {
    const cb = h("input", { type: "checkbox" });
    cb.checked = chosen.has(opt);
    cb.addEventListener("change", () => {
      if (cb.checked) chosen.add(opt); else chosen.delete(opt);
      onChange(Array.from(chosen));
    });
    box.append(h("label", { class: "check" }, cb, h("span", { text: opt })));
  }
  return box;
}

// Дропзона резюме: default (кнопка «upload») -> success (чип с именем файла + «✕») -> либо
// переключатель «ответить текстом» раскрывает textarea того же поля (UI-SPEC «Экран 5»).
// Прогресс/ошибка загрузки — забота вызывающего экрана (он владеет самим запросом
// POST /app/api/uploads); field() отдаёт сырой File через onChange, ничего не грузит сам
// (form.js не ходит в fetch — Reuse Contract).
function fileControl(h, spec, value, onChange) {
  const input = h("input", { type: "file", class: "hidden", accept: ".pdf,.doc,.docx" });
  const trigger = h("button", { class: "btn secondary dropzone-trigger", type: "button", onClick: () => input.click() }, icon("upload"));
  const status = h("span", { class: "dropzone-status" });
  const remove = h("button", {
    class: "btn ghost dropzone-remove hidden", type: "button", "aria-label": spec.label,
    onClick: () => { onChange(null); paint(null); },
  }, icon("x"));
  const textarea = h("textarea", { class: "input hidden", rows: "4" });
  const toggleText = h("button", {
    class: "btn ghost dropzone-toggle-text", type: "button", "aria-label": spec.label,
    onClick: () => textarea.classList.toggle("hidden"),
  }, icon("pen-line"));
  textarea.addEventListener("input", () => onChange({ text: textarea.value }));

  function paint(v) {
    const hasFile = Boolean(v && v.name);
    status.textContent = hasFile ? v.name : "";
    remove.classList.toggle("hidden", !hasFile);
    trigger.classList.toggle("hidden", hasFile);
  }
  input.addEventListener("change", () => {
    const file = (input.files || [])[0];
    input.value = "";
    if (!file) return;
    onChange(file);
    paint(file);
  });
  paint(value);
  return h("div", { class: "dropzone" }, trigger, status, remove, input, toggleText, textarea);
}

// Карточка согласия (pre-flow, не пронумерованный шаг мастера). Режим правки (locked, D-13) —
// забота setFieldState("disabled", …): чекбокс дизейблится и подпись даты согласия ставит
// вызывающий экран через payload, здесь — только структура.
function consentControl(h, spec, value, onChange) {
  const cb = h("input", { type: "checkbox" });
  cb.checked = Boolean(value);
  cb.addEventListener("change", () => onChange(cb.checked));
  return h("div", { class: "consent-card" },
    icon("shield-check"),
    h("label", { class: "check" }, cb, h("span", { text: spec.label })),
  );
}

function buildControl(h, spec, value, onChange) {
  switch (spec.type) {
    case "text":
      return textControl(h, spec, value, onChange, "text");
    case "textarea":
      return textareaControl(h, spec, value, onChange);
    case "phone":
      return textControl(h, spec, value, onChange, "tel", "tel");
    case "email":
      return textControl(h, spec, value, onChange, "email", "email");
    case "int":
      return intControl(h, spec, value, onChange);
    case "date":
      return dateControl(h, spec, value, onChange);
    case "choice-chips":
      return { control: choiceChips(h, spec, value, onChange) };
    case "select":
      return { control: selectControl(h, spec, value, onChange) };
    case "multi":
      return { control: multiControl(h, spec, value, onChange) };
    case "yesno":
      return { control: choiceChips(h, { ...spec, options: spec.options && spec.options.length ? spec.options : [] }, value, onChange) };
    case "file":
      return { control: fileControl(h, spec, value, onChange) };
    case "consent":
      return { control: consentControl(h, spec, value, onChange) };
    default:
      return textControl(h, spec, value, onChange, "text");
  }
}

/**
 * Рендер одного поля анкеты по контракту reg_engine.step_spec(): `.field` (label сверху,
 * контрол по spec.type, опциональный счётчик символов, зона инлайн-ошибки с
 * aria-live="polite"). Тот же вызов обслуживает и анкету (план 21-11), и веб-настройки
 * (фаза 22, Reuse Contract) — рендерер не переписывается по экранам, только расширяется
 * switch на новые типы (toggle/photo/list).
 * @param {(tag: string, attrs?: object, ...children: any[]) => HTMLElement} h
 * @param {{key: string, type: string, label: string, options?: string[], other_allowed?: boolean, max_len?: number}} spec
 * @param {*} value
 * @param {(v: *) => void} onChange
 */
export function field(h, spec, value, onChange) {
  const badge = h("span", { class: "chip accent-soft field-badge hidden" },
    icon("refresh-cw"), h("span", { class: "field-badge-text" }));
  const labelRow = h("div", { class: "field-label-row" },
    h("label", { text: spec.label, for: `f-${spec.key}` }),
    badge,
  );
  const { control, extra } = buildControl(h, spec, value, onChange);
  const placeholder = h("p", { class: "field-not-set hidden" });
  const errorZone = h("p", { class: "field-error hidden", "aria-live": "polite" });

  const wrap = h("div", { class: "field", "data-key": spec.key, "data-type": spec.type },
    labelRow, control, extra, placeholder, errorZone,
  );
  wrap._nodes = {
    control,
    badge,
    badgeText: badge.querySelector(".field-badge-text"),
    placeholder,
    errorZone,
  };
  return wrap;
}

function setControlDisabled(control, disabled) {
  if (!control) return;
  if ("disabled" in control) control.disabled = disabled;
  if (typeof control.querySelectorAll === "function") {
    control.querySelectorAll("input, select, textarea, button").forEach((node) => { node.disabled = disabled; });
  }
}

/**
 * Переключает состояние уже отрисованного `field()` (21-UI-SPEC.md § «Матрица состояний»):
 * default/focus/error/disabled/updated-in-chat/not-set. `focus` — не отдельная ветка, целиком
 * на CSS `:focus-visible`. Никаких новых DOM-узлов не создаёт — только показывает/прячет и
 * заполняет текстом узлы, уже построенные `field()`; весь человеческий текст приходит
 * параметром `payload.text` (сервер/реестр), не литералом.
 * @param {HTMLElement} el - узел, возвращённый field()
 * @param {"default"|"error"|"disabled"|"updated-in-chat"|"not-set"} state
 * @param {{text?: string}} [payload]
 */
export function setFieldState(el, state, payload) {
  const nodes = (el && el._nodes) || {};
  const data = payload || {};
  el.classList.remove("is-error", "is-disabled", "is-updated", "is-not-set");
  if (nodes.errorZone) { nodes.errorZone.textContent = ""; nodes.errorZone.classList.add("hidden"); }
  if (nodes.badge) nodes.badge.classList.add("hidden");
  if (nodes.placeholder) { nodes.placeholder.textContent = ""; nodes.placeholder.classList.add("hidden"); }
  setControlDisabled(nodes.control, false);

  if (state === "error") {
    el.classList.add("is-error");
    if (nodes.errorZone) { nodes.errorZone.textContent = data.text || ""; nodes.errorZone.classList.remove("hidden"); }
  } else if (state === "disabled") {
    el.classList.add("is-disabled");
    setControlDisabled(nodes.control, true);
  } else if (state === "updated-in-chat") {
    el.classList.add("is-updated");
    if (nodes.badge) {
      nodes.badge.classList.remove("hidden");
      if (nodes.badgeText) nodes.badgeText.textContent = data.text || "";
    }
  } else if (state === "not-set") {
    el.classList.add("is-not-set");
    if (nodes.placeholder) { nodes.placeholder.textContent = data.text || ""; nodes.placeholder.classList.remove("hidden"); }
  }
  // "default" — сброс выше уже достаточен.
}

// ── dirty/diff, confirm-box (Task 2) ───────────────────────────────────────────────────

/**
 * Локальное состояние формы поверх черновика: какие колонки правились локально (dirty), какой
 * патч отправлять (только изменённые), как подмешать пришедшие с сервера значения не перетирая
 * то, что человек правит прямо сейчас (D-19, `keepDirty`).
 * @param {Array<{column: string}>} specs
 * @param {Record<string, *>} values
 */
export function createFormState(specs, values) {
  const base = { ...(values || {}) };
  const current = { ...(values || {}) };
  const dirty = new Set();

  function isDirty(column) {
    return dirty.has(column);
  }
  function setValue(column, v) {
    current[column] = v;
    dirty.add(column);
  }
  function collectPatch() {
    const patch = {};
    for (const column of dirty) patch[column] = current[column];
    return patch;
  }
  // D-19: чужие правки из чата подмешиваются в base/current, НО не в поля, которые дельгат
  // правит прямо сейчас (keepDirty=true, дефолт) — возвращает список колонок, реально
  // подменённых (для setFieldState("updated-in-chat", …) на стороне экрана).
  function applyServer(fresh, opts) {
    const keepDirty = !opts || opts.keepDirty !== false;
    const updated = [];
    for (const [column, v] of Object.entries(fresh || {})) {
      if (keepDirty && dirty.has(column)) continue;
      if (base[column] !== v) updated.push(column);
      base[column] = v;
      current[column] = v;
    }
    return updated;
  }
  function reset() {
    dirty.clear();
    for (const column of Object.keys(base)) current[column] = base[column];
  }
  function value(column) {
    return current[column];
  }

  return { specs: specs || [], isDirty, setValue, collectPatch, applyServer, reset, value, base, current };
}

/**
 * Строки «Было: {X}» + бейдж «изменено» под каждым локально изменённым полем (обзор правки,
 * D-26, UI-SPEC «Экран 7»). Подписи — параметром `labels` (реестр), не литералом.
 * @param {*} h
 * @param {Record<string,*>} base
 * @param {Record<string,*>} current
 * @param {{changedBadgeText?: string, wasPrefix?: string}} labels
 */
export function diffView(h, base, current, labels) {
  const box = h("div", { class: "diff-view" });
  const l = labels || {};
  for (const [column, from] of Object.entries(base || {})) {
    const to = current ? current[column] : undefined;
    if (to === undefined || to === from) continue;
    box.append(h("div", { class: "diff-row" },
      l.changedBadgeText ? h("span", { class: "chip accent", text: l.changedBadgeText }) : null,
      h("p", { class: "label-role diff-was" }, `${l.wasPrefix || ""}${from == null ? "" : String(from)}`),
    ));
  }
  return box;
}

/**
 * Обобщение существующего `.confirm-box` (task_edit.js/settings.js) — все три надписи приходят
 * снаружи (сервер/реестр), как в settings.js::openConfirm. Возвращает узел с методами
 * open()/close() поверх готовой разметки.
 * @param {*} h
 * @param {{text?: string, confirmText?: string, cancelText?: string, onConfirm?: () => void, onCancel?: () => void}} opts
 */
export function confirmBox(h, opts) {
  const o = opts || {};
  const box = h("div", { class: "confirm-box hidden" },
    h("p", { text: o.text || "" }),
    h("button", { class: "btn danger", type: "button", text: o.confirmText || "", onClick: () => { if (o.onConfirm) o.onConfirm(); } }),
    h("button", {
      class: "btn ghost", type: "button", text: o.cancelText || "",
      onClick: () => { box.classList.add("hidden"); if (o.onCancel) o.onCancel(); },
    }),
  );
  box.open = () => box.classList.remove("hidden");
  box.close = () => box.classList.add("hidden");
  return box;
}

// Список-чипы (построены в 21, потребитель — фаза 22, WEB-SET-01: у анкеты нет type=list
// шагов). Добавление — Enter, запятая, «;»-разделитель при вставке (память проекта
// «telegram-enter-send-trap» — списки обязаны принимать «;» как разделитель).
export function listChips(h, opts) {
  const o = opts || {};
  let items = Array.isArray(o.values) ? [...o.values] : [];
  const input = h("input", { class: "input", type: "text", placeholder: o.placeholder || "" });
  const box = h("div", { class: "list-chips" });

  function emit() {
    if (o.onChange) o.onChange([...items]);
  }

  function paint() {
    box.replaceChildren(
      ...items.map((v, idx) => h("span", { class: "chip list-chip" },
        h("span", { text: v }),
        h("button", {
          class: "chip-remove", type: "button", "aria-label": o.placeholder || v,
          onClick: () => { items.splice(idx, 1); paint(); emit(); },
        }, icon("x")),
      )),
      input,
    );
  }

  function addFromText(raw) {
    const parts = String(raw || "").split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);
    if (!parts.length) return;
    items.push(...parts);
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "," || e.key === ";") {
      e.preventDefault();
      addFromText(input.value);
      input.value = "";
      paint();
      emit();
    }
  });
  input.addEventListener("paste", (e) => {
    const clip = e.clipboardData || window.clipboardData;
    const text = clip ? clip.getData("text") : "";
    if (text && /[,;\n]/.test(text)) {
      e.preventDefault();
      addFromText(text);
      input.value = "";
      paint();
      emit();
    }
  });

  paint();
  return box;
}

// ── headless-утилиты для фазы 22 (поиск/свёртка группы, без DOM) ─────────────────────────

function normalizeSearch(s) {
  return String(s || "").toLowerCase().replace(/ё/g, "е");
}

/**
 * Нестрогое совпадение по подписи/подсказке/значению с нормализацией регистра и «ё» (поиск по
 * ~70 ключам реестра, WEB-SET-02). Потребителя в фазе 21 нет — headless-функция строится и
 * тестируется здесь, UI над ней подключает фаза 22.
 * @param {Array<{label?: string, prompt?: string, value?: *}>} items
 * @param {string} query
 */
export function searchFilter(items, query) {
  const q = normalizeSearch(query).trim();
  if (!q) return items || [];
  return (items || []).filter((item) => {
    const haystack = [item.label, item.prompt, item.value].map(normalizeSearch).join(" ");
    return haystack.includes(q);
  });
}

/**
 * Переключает свёртку группы настроек (WEB-SET-02) — чистая функция, возвращает НОВЫЙ объект
 * состояния (не мутирует `state`), чтобы вызывающий экран мог сравнивать ссылки при перерисовке.
 * @param {Record<string, boolean>} state
 * @param {string} groupCode
 */
export function groupCollapse(state, groupCode) {
  const next = { ...(state || {}) };
  next[groupCode] = !next[groupCode];
  return next;
}
