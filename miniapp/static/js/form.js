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
import { flatRow } from "./ui.js";

// Фаза 22 (D-05, Reuse Contract 21-UI-SPEC): тот же рендерер обслуживает реестр настроек —
// ветки toggle/photo/list, адаптер settingSpec(), нестрогий поиск (D-15). Модуль остаётся
// импортируемым в чистом node (поведенческий тест поиска): на уровне модуля нет обращений
// к document/window — DOM только внутри функций, вызываемых экраном.

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
// (form.js не ходит в сеть — Reuse Contract).
//
// Фаза 22 (D-05): та же дропзона обслуживает photo/file реестра — settingSpec() выключает
// «ответить текстом» (spec.text_allowed=false), задаёт accept, а сохранённое значение
// показывается миниатюрой (spec.preview_url) либо именем (spec.display) — сырой file_id
// человеку не показывается (T-19-45). Состояния uploading/ошибка — setFieldState()
// с текстом из payload (узел progress отдаётся наверх).
function localPreviewUrl(file) {
  if (!file || typeof file.type !== "string" || !file.type.startsWith("image/")) return null;
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return null;
  return URL.createObjectURL(file);
}

function fileControl(h, spec, value, onChange) {
  const textAllowed = spec.text_allowed !== false;
  const input = h("input", { type: "file", class: "hidden", accept: spec.accept || ".pdf,.doc,.docx" });
  const trigger = h("button", {
    class: "btn secondary dropzone-trigger", type: "button", "aria-label": spec.label, onClick: () => input.click(),
  }, icon(spec.type === "photo" ? "image" : "upload"));
  const preview = h("img", { class: "dropzone-preview hidden", alt: "" });
  const status = h("span", { class: "dropzone-status" });
  const progress = h("span", { class: "dropzone-progress hidden", "aria-live": "polite" });
  const remove = h("button", {
    class: "btn ghost dropzone-remove hidden", type: "button", "aria-label": spec.label,
    onClick: () => { onChange(null); paint(null); },
  }, icon("x"));
  const textarea = textAllowed ? h("textarea", { class: "input hidden", rows: "4" }) : null;
  const toggleText = textAllowed ? h("button", {
    class: "btn ghost dropzone-toggle-text", type: "button", "aria-label": spec.label,
    onClick: () => textarea.classList.toggle("hidden"),
  }, icon("pen-line")) : null;
  if (textarea) textarea.addEventListener("input", () => onChange({ text: textarea.value }));

  function paint(v) {
    const local = Boolean(v && v.name);
    const stored = !local && Boolean((v != null && v !== "") || spec.preview_url);
    status.textContent = local ? v.name : (stored ? (spec.display || "") : "");
    const src = local ? localPreviewUrl(v) : (stored ? spec.preview_url : null);
    if (src) {
      preview.src = src;
      preview.classList.remove("hidden");
    } else {
      preview.removeAttribute("src");
      preview.classList.add("hidden");
    }
    remove.classList.toggle("hidden", !(local || stored));
    trigger.classList.toggle("hidden", local);
  }
  input.addEventListener("change", () => {
    const file = (input.files || [])[0];
    input.value = "";
    if (!file) return;
    onChange(file);
    paint(file);
  });
  paint(value);
  const control = h("div", { class: "dropzone" }, preview, trigger, status, progress, remove, input, toggleText, textarea);
  return { control, progress };
}

// Тумблер реестра (toggle, D-05/D-08, D-17 Task 1): визуал сегодняшних «настроек-лайт» —
// flatRow с классом check-row (ui.js), подписи состояний из spec.texts ({on, off} — реестр),
// не литералом. КОНТРОЛ САМ красит своё состояние по клику (оптимистично, до ответа сети) —
// `repaintRow` остаётся авторитетным ПОСЛЕ ответа сервера (перестраивает контрол заново через
// `paint(value)` в конструкторе); `control.paint(v)` снаружи (например, откат по ошибке
// сохранения в screens/settings.js::saveToggle) переигрывает визуал и синхронизирует
// внутренний `current`, чтобы следующий клик снова считал правильное «следующее» значение.
function isOn(v) {
  return v === true || v === "on";
}

function toggleControl(h, spec, value, onChange) {
  const texts = spec.texts || {};
  let current = value;
  const row = flatRow(h, {
    icon: "check", title: spec.label, trailing: "", cls: "check-row",
    onClick: () => {
      const next = isOn(current) ? "off" : "on";
      paint(next);
      onChange(next);
    },
  });
  const trailing = row.querySelector(".flat-row-trailing");
  function paint(v) {
    current = v;
    const on = isOn(v);
    row.classList.toggle("on", on);
    row.setAttribute("aria-pressed", on ? "true" : "false");
    if (trailing) trailing.textContent = on ? (texts.on || "") : (texts.off || "");
  }
  row.paint = paint;
  paint(value);
  return row;
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
      return fileControl(h, spec, value, onChange);
    case "consent":
      return { control: consentControl(h, spec, value, onChange) };
    // Типы реестра настроек (фаза 22, D-05) — у анкеты их нет.
    case "toggle":
      return { control: toggleControl(h, spec, value, onChange) };
    case "photo":
      // Список расширений, а не MIME-маска image со звёздочкой: слэш-звёздочка внутри
      // строки читается сторожем тестов как начало блочного комментария.
      return fileControl(h, { ...spec, accept: spec.accept || ".jpg,.jpeg,.png,.webp", text_allowed: false }, value, onChange);
    case "list":
      return { control: listChips(h, { values: value, onChange, placeholder: spec.placeholder || "" }) };
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
 * Фаза 22: `spec.help` (подсказка реестра) рисуется под подписью всегда (A5 22-UI-SPEC);
 * у `toggle` подпись живёт внутри строки-тумблера, отдельный label скрыт. Узлы `label`/`help`
 * отдаются в `_nodes`, чтобы экран мог подменить их детей подсветкой `highlightMatch()`.
 * @param {{key: string, type: string, label: string, help?: string, options?: string[], other_allowed?: boolean, max_len?: number}} spec
 * @param {*} value
 * @param {(v: *) => void} onChange
 */
export function field(h, spec, value, onChange) {
  const badge = h("span", { class: "chip accent-soft field-badge hidden" },
    icon("refresh-cw"), h("span", { class: "field-badge-text" }));
  const label = h("label", { text: spec.label, for: `f-${spec.key}`, class: spec.type === "toggle" ? "hidden" : null });
  const labelRow = h("div", { class: "field-label-row" }, label, badge);
  const help = spec.help ? h("p", { class: "field-help label-role", text: spec.help }) : null;
  const { control, extra, progress } = buildControl(h, spec, value, onChange);
  const placeholder = h("p", { class: "field-not-set hidden" });
  const errorZone = h("p", { class: "field-error hidden", "aria-live": "polite" });

  const wrap = h("div", { class: "field", "data-key": spec.key, "data-type": spec.type },
    labelRow, help, control, extra, placeholder, errorZone,
  );
  wrap._nodes = {
    control,
    badge,
    badgeText: badge.querySelector(".field-badge-text"),
    placeholder,
    errorZone,
    label,
    help,
    progress: progress || null,
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
 * Фаза 22: `uploading` — загрузка photo/file реестра в полёте (контрол заблокирован, текст
 * прогресса из `payload.text` в узле progress дропзоны); ошибка загрузки — обычный `error`,
 * дропзона остаётся кликабельной для повтора.
 * @param {HTMLElement} el - узел, возвращённый field()
 * @param {"default"|"error"|"disabled"|"updated-in-chat"|"not-set"|"uploading"} state
 * @param {{text?: string}} [payload]
 */
export function setFieldState(el, state, payload) {
  const nodes = (el && el._nodes) || {};
  const data = payload || {};
  el.classList.remove("is-error", "is-disabled", "is-updated", "is-not-set", "is-uploading");
  if (nodes.errorZone) { nodes.errorZone.textContent = ""; nodes.errorZone.classList.add("hidden"); }
  if (nodes.badge) nodes.badge.classList.add("hidden");
  if (nodes.placeholder) { nodes.placeholder.textContent = ""; nodes.placeholder.classList.add("hidden"); }
  if (nodes.progress) { nodes.progress.textContent = ""; nodes.progress.classList.add("hidden"); }
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
  } else if (state === "uploading") {
    el.classList.add("is-uploading");
    setControlDisabled(nodes.control, true);
    if (nodes.progress) { nodes.progress.textContent = data.text || ""; nodes.progress.classList.remove("hidden"); }
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
  // D9 (quick 260904-de4): правка, которую уже применил сервер (файл резюме — загружен через
  // POST /uploads?target=resume, значение лежит в черновике), — это dirty ДЛЯ КНОПКИ
  // «Отправить изменения», но НЕ дельта для PATCH: повторная отправка текстом затёрла бы
  // файл. Отдельный набор, а не общий `dirty`, — collectPatch() не должен видеть эти колонки.
  const serverDirty = new Set();

  function isDirty(column) {
    return dirty.has(column) || serverDirty.has(column);
  }
  function markServerDirty(column) {
    serverDirty.add(column);
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
  // подменённых (для setFieldState("updated-in-chat", …) на стороне экрана). Проверяет только
  // `dirty` (не `serverDirty`): столбец, только что записанный сервером (файл), обязан принять
  // свежее значение сервера, а не защищаться как «своя локальная» правка.
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
    serverDirty.clear();
    for (const column of Object.keys(base)) current[column] = base[column];
  }
  function value(column) {
    return current[column];
  }

  return { specs: specs || [], isDirty, markServerDirty, setValue, collectPatch, applyServer, reset, value, base, current };
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

// ── settingSpec: элемент ответа settings/all → spec для field() (фаза 22, D-05) ──────────

// Порог A2 22-UI-SPEC: текст с max_len выше этого — textarea с автовысотой (у анкеты то же
// деление: 200 у ФИО против 1000/4000 у свободных текстов).
const SETTING_TEXT_INPUT_MAX_LEN = 200;
// Порог A3 21-UI-SPEC: enum до 4 вариантов — сегмент чипов, больше — нативный select.
const SETTING_CHIPS_MAX_OPTIONS = 4;

function isLongSettingText(item) {
  if (item.max_len) return item.max_len > SETTING_TEXT_INPUT_MAX_LEN;
  // Шаблонные/HTML-тексты бота без лимита (item.html) — многострочные по природе.
  return Boolean(item.html);
}

// D-17 Task 1 — options ровно {"on","off"} (порядок неважен, значений ровно два).
function isOnOffEnum(options) {
  const opts = options || [];
  return opts.length === 2 && opts.includes("on") && opts.includes("off");
}

/**
 * Чистый адаптер «элемент ответа API настроек → spec для field()». Тип реестра → тип
 * рендера: text → text/textarea (порог max_len / HTML-ключи), enum → choice-chips/select
 * (порог 4), int/date/toggle/photo/file/list — один в один. label/help/options/max_len
 * переносятся как есть — ни одной подписи адаптер не сочиняет (D-13): тексты состояний
 * тумблера (item.texts), display/preview_url файла, placeholder списка тоже приходят с сервера.
 * @param {{key: string, type: string, label: string, help?: string, options?: string[], max_len?: number, html?: boolean, texts?: {on?: string, off?: string}, display?: string, preview_url?: string, accept?: string, placeholder?: string}} item
 */
export function settingSpec(item) {
  const it = item || {};
  const spec = { key: it.key, type: "text", label: it.label, help: it.help, options: it.options, max_len: it.max_len };
  switch (it.type) {
    case "text":
      spec.type = isLongSettingText(it) ? "textarea" : "text";
      break;
    case "enum":
      // D-17 Task 1 (владелец 03.09): enum ровно с options ["on","off"] — тот же тумблер-
      // слайдер, что настоящий тип "toggle" (reg_q_*), а не chip-кнопки "on"/"off": человеку
      // это один и тот же вопрос «включено или нет», два разных контрола для него сбивали с
      // толку («настройки как будто разные»). Опасные ключи (registration_mode и т.п. сюда
      // не попадают: у них options — НЕ ["on","off"]) confirm-flow не теряют — экран решает
      // это по item.confirm_text, не по типу контрола.
      spec.type = isOnOffEnum(it.options)
        ? "toggle"
        : (it.options || []).length <= SETTING_CHIPS_MAX_OPTIONS ? "choice-chips" : "select";
      break;
    case "int":
    case "date":
    case "toggle":
    case "photo":
    case "file":
    case "list":
      spec.type = it.type;
      break;
    default:
      break;
  }
  if (spec.type === "toggle") spec.texts = it.texts;
  if (spec.type === "photo" || spec.type === "file") {
    spec.display = it.display;
    spec.preview_url = it.preview_url;
    spec.accept = it.accept;
    spec.text_allowed = false;
  }
  if (spec.type === "list") spec.placeholder = it.placeholder;
  return spec;
}

// ── headless-утилиты для фазы 22 (поиск/свёртка группы, без DOM) ─────────────────────────

// Нестрогий поиск по реестру (WEB-SET-02, D-15б/в), своим кодом без внешних библиотек:
// подпись + подсказка + синонимы с сервера (search_terms — словарь во фронте не живёт, D-13)
// + текущее отображаемое значение; нижний регистр, ё=е; запрос режется на слова, совпасть
// должны ВСЕ (AND); слово совпадает по префиксу слова кандидата, а с 5 символов — ещё и с
// одной опечаткой (Левенштейн ≤ 1, ранний выход — T-22-10: 255 ключей на каждый keystroke).

// Опечатка допускается только у слов от этой длины — короче слишком много ложных попаданий.
const SEARCH_FUZZY_MIN_LEN = 5;
// Подсказка при нуле результатов (D-15в): ближайшие слова по тому же расстоянию, порог 2.
const SEARCH_SUGGEST_MAX_DISTANCE = 2;
const SEARCH_SUGGEST_MIN_WORD_LEN = 3;
const SEARCH_SUGGEST_DEFAULT_LIMIT = 3;

function normalizeSearch(s) {
  // U+0451 (ё) -> U+0435 (е) кодовыми точками, не литералом кириллицы (D-25, сторож
  // test_form_js_has_no_human_text_literals сканирует ВСЕ строковые литералы файла).
  // Обе замены сохраняют длину строки — индексы токенов годятся для подсветки оригинала.
  return String(s == null ? "" : s).toLowerCase().replace(/\u0451/g, "\u0435");
}

const SEARCH_WORD_RE = /[\p{L}\p{N}]+/gu;

// Слова текста с позициями в исходной строке.
function searchTokens(text) {
  const out = [];
  const norm = normalizeSearch(text);
  for (const m of norm.matchAll(SEARCH_WORD_RE)) {
    out.push({ word: m[0], start: m.index, end: m.index + m[0].length });
  }
  return out;
}

function searchQueryWords(query) {
  return searchTokens(query).map((t) => t.word);
}

// Расстояние Левенштейна ≤ 1? Один проход с ранним выходом: расхождение длин больше единицы
// отсекается сразу, дальше сравниваются хвосты после первого несовпадения.
function withinOneEdit(a, b) {
  if (a === b) return true;
  const la = a.length;
  const lb = b.length;
  if (Math.abs(la - lb) > 1) return false;
  const n = Math.min(la, lb);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  if (la === lb) return a.slice(i + 1) === b.slice(i + 1);
  if (la > lb) return a.slice(i + 1) === b.slice(i);
  return a.slice(i) === b.slice(i + 1);
}

// Совпадение слова запроса с одним словом кандидата: сначала префикс, затем — только для
// слов от SEARCH_FUZZY_MIN_LEN и только если префикс не сработал — одна опечатка против слова
// целиком или его префикса той же длины. Возвращает длину совпавшего отрезка (0 — мимо).
function matchSearchWord(q, word) {
  if (word.startsWith(q)) return q.length;
  if (q.length >= SEARCH_FUZZY_MIN_LEN) {
    if (withinOneEdit(q, word)) return word.length;
    if (word.length > q.length && withinOneEdit(q, word.slice(0, q.length))) return q.length;
  }
  return 0;
}

// Токены полей элемента; кэш по объекту элемента — массив от экрана между нажатиями клавиш
// один и тот же, токенизировать 255 ключей заново на каждую букву незачем.
const searchWordsCache = new WeakMap();

function searchFieldWords(item) {
  const it = item || {};
  const cacheable = typeof item === "object" && item !== null;
  if (cacheable && searchWordsCache.has(item)) return searchWordsCache.get(item);
  const terms = Array.isArray(it.search_terms) ? it.search_terms.join(" ") : (it.search_terms || "");
  const value = it.display != null ? it.display : it.value;
  const valueText = Array.isArray(value) ? value.join(" ") : (value == null ? "" : String(value));
  const words = {
    label: searchTokens(it.label),
    help: searchTokens(it.help != null ? it.help : it.prompt),
    other: searchTokens(`${terms} ${valueText}`),
  };
  if (cacheable) searchWordsCache.set(item, words);
  return words;
}

/**
 * Нестрогий поиск по элементам реестра (контракт D-15б). Пустой запрос — все элементы в
 * исходном порядке. Возвращает `{item, ranges}`: `ranges.label` — совпавшие отрезки
 * `[start, end)` в подписи, `ranges.help` — в подсказке (только для слов, не найденных в
 * подписи); совпадения по синонимам/значению отрезков не дают. Подсветку по `ranges` рисует
 * вызывающий код через highlightMatch().
 * @param {Array<{label?: string, help?: string, prompt?: string, search_terms?: string[], value?: *, display?: string}>} items
 * @param {string} query
 * @returns {Array<{item: object, ranges: {label: number[][], help: number[][]}}>}
 */
export function searchFilter(items, query) {
  const list = items || [];
  const words = searchQueryWords(query);
  if (!words.length) return list.map((item) => ({ item, ranges: { label: [], help: [] } }));
  const out = [];
  for (const item of list) {
    const fields = searchFieldWords(item);
    const ranges = { label: [], help: [] };
    let all = true;
    for (const q of words) {
      let hit = false;
      for (const t of fields.label) {
        const n = matchSearchWord(q, t.word);
        if (n) { ranges.label.push([t.start, t.start + n]); hit = true; }
      }
      if (!hit) {
        for (const t of fields.help) {
          const n = matchSearchWord(q, t.word);
          if (n) { ranges.help.push([t.start, t.start + n]); hit = true; }
        }
      }
      if (!hit) hit = fields.other.some((t) => matchSearchWord(q, t.word) > 0);
      if (!hit) { all = false; break; }
    }
    if (all) out.push({ item, ranges });
  }
  return out;
}

/**
 * Подпись с подсветкой совпадений — массив детей для h(): обычные отрезки строками
 * (textContent), совпавшие — h("mark"). Пересекающиеся отрезки склеиваются. Разметка
 * строкой не собирается (T-22-03: в подписи может оказаться ввод менеджера).
 * @param {*} h
 * @param {string} text
 * @param {number[][]} ranges - отрезки [start, end) из searchFilter()
 */
export function highlightMatch(h, text, ranges) {
  const s = text == null ? "" : String(text);
  const sorted = (ranges || [])
    .map(([a, b]) => [Math.max(0, a), Math.min(s.length, b)])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  const merged = [];
  for (const r of sorted) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  const out = [];
  let pos = 0;
  for (const [a, b] of merged) {
    if (a > pos) out.push(s.slice(pos, a));
    out.push(h("mark", { text: s.slice(a, b) }));
    pos = b;
  }
  if (pos < s.length) out.push(s.slice(pos));
  return out;
}

// Ограниченное расстояние Левенштейна (порог max, ранний выход, когда вся строка матрицы
// выше порога). Зовётся только при нуле результатов — не на каждое нажатие клавиши.
function levenshteinBounded(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = [];
  for (let j = 0; j <= b.length; j++) prev.push(j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let rowMin = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      const v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      cur.push(v);
      if (v < rowMin) rowMin = v;
    }
    if (rowMin > max) return max + 1;
    prev = cur;
  }
  return prev[b.length];
}

/**
 * Подсказка при нуле результатов (D-15в): до `limit` ближайших слов из подписей и синонимов
 * (по тому же расстоянию Левенштейна, порог 2; слово, начинающееся с запроса, ближе любой
 * опечатки), отсортированных по близости. Возвращает слова, не тексты — формулировку
 * «возможно, вы имели в виду» даёт реестр.
 * @param {Array<{label?: string, search_terms?: string[]}>} items
 * @param {string} query
 * @param {number} [limit]
 * @returns {string[]}
 */
export function suggestTerms(items, query, limit) {
  const words = searchQueryWords(query);
  const cap = limit == null ? SEARCH_SUGGEST_DEFAULT_LIMIT : limit;
  if (!words.length || cap <= 0) return [];
  const vocab = new Set();
  for (const item of items || []) {
    const it = item || {};
    for (const t of searchTokens(it.label)) vocab.add(t.word);
    for (const term of Array.isArray(it.search_terms) ? it.search_terms : []) {
      for (const t of searchTokens(term)) vocab.add(t.word);
    }
  }
  const scored = [];
  for (const w of vocab) {
    if (w.length < SEARCH_SUGGEST_MIN_WORD_LEN) continue;
    let best = Infinity;
    for (const q of words) {
      if (w === q) { best = Infinity; break; }
      const d = w.startsWith(q)
        ? (w.length - q.length) / (w.length + 1)
        : levenshteinBounded(q, w, SEARCH_SUGGEST_MAX_DISTANCE);
      if (d < best) best = d;
    }
    if (best <= SEARCH_SUGGEST_MAX_DISTANCE) scored.push([best, w]);
  }
  scored.sort((x, y) => x[0] - y[0] || (x[1] < y[1] ? -1 : (x[1] > y[1] ? 1 : 0)));
  return scored.slice(0, cap).map(([, w]) => w);
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
