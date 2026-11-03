// Конструктор условий правила: блоки «ИЛИ», внутри блока условия через «и».
// Коды вопросов и операторов живут только в объекте groups. На экран — подписи каталога.
// Модуль ничего не сохраняет: после правки зовёт onChange с новым массивом.

import { icon } from "./icons.js";
import { openSheet } from "./rr_sheet.js";
import { confirmBox, highlightMatch, searchFilter } from "./form.js";
import { buildV2Control } from "./form_types.js";

const NO_VALUE = new Set(["filled", "empty", "has_file", "no_file"]);
const DATE_OPS = new Set(["before", "after"]);
const ERR_STEP = "Выберите вопрос";
const ERR_OP = "Выберите, как сравнить";
const ERR_VALUES = "Выберите хотя бы одно значение.";
const ERR_INT = "Нужно целое число, например 18.";
const ERR_DATE = "Не понял дату. Выберите её в календаре или введите как 31.12.2026.";
const ERR_OFF = "Этот вопрос выключен";

function blank() {
  return { step: "", op: "", values: [] };
}

function clone(groups) {
  return (groups || []).map((group) => (group || []).map((cond) => ({
    step: cond && cond.step ? String(cond.step) : "",
    op: cond && cond.op ? String(cond.op) : "",
    values: [...((cond && cond.values) || [])].map((value) => String(value)),
  })));
}

function stepMap(catalog) {
  const map = new Map();
  for (const step of (catalog && catalog.steps) || []) map.set(step.step, step);
  return map;
}

function takesValues(op) {
  return !NO_VALUE.has(op);
}

function isIntOp(spec, op) {
  if (!spec) return false;
  if (spec.category === "int") return true;
  return spec.category === "birth_date" && !DATE_OPS.has(op);
}

function isDateOp(spec, op) {
  if (!spec) return false;
  if (spec.category === "date") return true;
  return spec.category === "birth_date" && DATE_OPS.has(op);
}

function integerOk(text) {
  return /^-?\d+$/.test(String(text).trim());
}

function dateOk(text) {
  const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(String(text).trim());
  if (!match) return false;
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

function dmyToIso(text) {
  const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(String(text || "").trim());
  return match ? `${match[3]}-${match[2]}-${match[1]}` : "";
}

function isoToDmy(text) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || "").trim());
  return match ? `${match[3]}.${match[2]}.${match[1]}` : "";
}

function filled(values) {
  return (values || []).map((value) => String(value).trim()).filter(Boolean);
}

export function validateGroups(groups, catalog) {
  const errors = new Map();
  const steps = stepMap(catalog);
  (groups || []).forEach((group, g) => {
    (group || []).forEach((cond, c) => {
      const key = `${g}:${c}`;
      const step = cond && cond.step;
      if (!step) {
        errors.set(key, ERR_STEP);
        return;
      }
      const spec = steps.get(step);
      if (!spec) {
        errors.set(key, ERR_OFF);
        return;
      }
      const ops = spec.operators || [];
      if (!cond.op || !ops.some((item) => item.op === cond.op)) {
        errors.set(key, ERR_OP);
        return;
      }
      if (!takesValues(cond.op)) return;
      const values = filled(cond.values);
      if (!values.length || (cond.op === "between" && values.length < 2)) {
        errors.set(key, ERR_VALUES);
        return;
      }
      if (isIntOp(spec, cond.op)) {
        const nums = cond.op === "between" ? values.slice(0, 2) : [values[0]];
        if (nums.some((value) => !integerOk(value))) errors.set(key, ERR_INT);
        return;
      }
      if (isDateOp(spec, cond.op) && !dateOk(values[0])) errors.set(key, ERR_DATE);
    });
  });
  return { ok: errors.size === 0, errors };
}

function hasRows(groups) {
  return (groups || []).some((group) => group && group.length);
}

export function renderConditions(h, opts) {
  const state = {
    groups: clone(opts.groups),
    catalog: opts.catalog || { steps: [] },
    cityLabel: opts.cityLabel || "",
    forumDate: opts.forumDate == null ? null : opts.forumDate,
    readOnly: Boolean(opts.readOnly),
    onChange: typeof opts.onChange === "function" ? opts.onChange : () => {},
  };
  const el = h("div", { class: "rr-conditions" });
  const anchors = new Map();

  function publish(next) {
    state.groups = clone(next);
    state.onChange(clone(state.groups));
    paint();
  }

  function patch(g, c, part, repaint) {
    const next = clone(state.groups);
    next[g][c] = { ...next[g][c], ...part };
    state.groups = next;
    state.onChange(clone(next));
    if (repaint) paint();
    else refreshError(g, c);
  }

  function refreshError(g, c) {
    const slot = el.querySelector(`[data-rr-error="${g}:${c}"]`);
    if (!slot) return;
    const text = validateGroups(state.groups, state.catalog).errors.get(`${g}:${c}`) || "";
    slot.textContent = text;
    if (text) slot.removeAttribute("hidden");
    else slot.setAttribute("hidden", "");
  }

  function viewKey() {
    return JSON.stringify({
      groups: state.groups,
      steps: (state.catalog && state.catalog.steps) || [],
      cityLabel: state.cityLabel,
      forumDate: state.forumDate,
      readOnly: state.readOnly,
    });
  }

  function soleOp(cond) {
    if (!cond || !cond.step) return "";
    const spec = stepMap(state.catalog).get(cond.step);
    const ops = (spec && spec.operators) || [];
    return ops.length === 1 ? String(ops[0].op || "") : "";
  }

  // Единственный оператор на экране скрыт, поэтому в объект он попадает здесь,
  // а не отдельным шагом. Повторный update с тем же снимком не перерисовывает поле.
  function normalized(groups) {
    let changed = false;
    const next = groups.map((group) => group.map((cond) => {
      const only = soleOp(cond);
      if (!only || cond.op === only) return cond;
      changed = true;
      return { ...cond, op: only, values: cond.op ? [] : [...cond.values] };
    }));
    return { groups: next, changed };
  }

  function update(groups, catalog, ctx2) {
    const before = viewKey();
    if (catalog) state.catalog = catalog;
    if (ctx2) {
      if (Object.prototype.hasOwnProperty.call(ctx2, "cityLabel")) state.cityLabel = ctx2.cityLabel || "";
      if (Object.prototype.hasOwnProperty.call(ctx2, "forumDate")) state.forumDate = ctx2.forumDate == null ? null : ctx2.forumDate;
      if (Object.prototype.hasOwnProperty.call(ctx2, "readOnly")) state.readOnly = Boolean(ctx2.readOnly);
    }
    const applied = normalized(clone(groups));
    state.groups = applied.groups;
    if (viewKey() === before) return;
    if (applied.changed && !state.readOnly) state.onChange(clone(state.groups));
    paint();
  }

  function focusFirstError() {
    const first = validateGroups(state.groups, state.catalog).errors.keys().next();
    if (first.done) return false;
    const node = anchors.get(first.value);
    if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "center" });
    if (node && typeof node.focus === "function") node.focus();
    return true;
  }

  function paint() {
    anchors.clear();
    const errors = validateGroups(state.groups, state.catalog).errors;
    const lead = h("p", {
      class: "rr-lead",
      text: "Заявка попадает под правило, если в одном из блоков подходят ВСЕ условия.",
    });
    if (!hasRows(state.groups)) {
      el.replaceChildren(lead, emptyState());
      return;
    }
    const blocks = [];
    state.groups.forEach((group, g) => {
      if (g > 0) {
        blocks.push(h("div", { class: "rr-or", role: "separator", "aria-label": "или", text: "ИЛИ" }));
      }
      blocks.push(blockCard(group, g, errors));
    });
    if (!state.readOnly) {
      blocks.push(h("button", {
        type: "button",
        class: "btn secondary",
        text: "+ Добавить блок «или»",
        onClick: () => publish([...clone(state.groups), [blank()]]),
      }));
    }
    el.replaceChildren(lead, ...blocks);
  }

  function emptyState() {
    return h("div", {},
      h("p", {
        text: "Условий пока нет. Добавьте первое — например, «Возраст на дату форума — меньше 18»",
      }),
      state.readOnly ? null : h("button", {
        type: "button",
        class: "btn secondary",
        text: "+ Добавить условие",
        onClick: () => publish([[blank()]]),
      }),
    );
  }

  function blockCard(group, g, errors) {
    const n = g + 1;
    const head = h("div", { class: "rr-block-head" },
      h("span", { class: "rr-hint", text: `Блок ${n}` }),
      state.readOnly ? null : h("button", {
        type: "button",
        class: "btn ghost",
        "aria-label": `Убрать блок ${n}`,
        onClick: () => askRemoveBlock(g),
      }, icon("trash-2")),
    );
    const rows = [];
    (group || []).forEach((cond, c) => {
      if (c > 0) rows.push(h("div", { class: "rr-cond__and", text: "и" }));
      rows.push(conditionRow(cond, g, c, errors));
    });
    const add = state.readOnly ? null : h("button", {
      type: "button",
      class: "btn ghost",
      text: "+ и ещё условие",
      onClick: () => {
        const next = clone(state.groups);
        next[g] = [...next[g], blank()];
        publish(next);
      },
    });
    return h("div", { class: "rr-card" }, head, ...rows, add);
  }

  function askRemoveBlock(g) {
    const count = (state.groups[g] || []).length;
    if (count < 2) {
      dropBlock(g);
      return;
    }
    let sheet = null;
    const box = confirmBox(h, {
      text: `Убрать блок вместе с ${count} условиями? Сохранённое правило не изменится, пока вы не нажмёте «Сохранить правило».`,
      confirmText: "Убрать блок",
      cancelText: "Оставить блок",
      onConfirm: () => {
        if (sheet) sheet.close();
        dropBlock(g);
      },
      onCancel: () => { if (sheet) sheet.close(); },
    });
    box.open();
    sheet = openSheet(h, {
      title: "Убрать блок?",
      body: box,
      actions: [],
    });
    const keep = box.querySelector(".btn.ghost");
    if (keep) keep.focus();
  }

  function dropBlock(g) {
    const next = clone(state.groups);
    next.splice(g, 1);
    publish(next.filter((group) => group.length));
  }

  function dropCondition(g, c) {
    const next = clone(state.groups);
    next[g] = next[g].filter((_, index) => index !== c);
    if (!next[g].length) {
      if (next.length > 1) next.splice(g, 1);
      else return publish([]);
    }
    publish(next);
  }

  function conditionRow(cond, g, c, errors) {
    const key = `${g}:${c}`;
    const spec = stepMap(state.catalog).get(cond.step);
    const missing = Boolean(cond.step) && !spec;
    const label = spec ? (spec.label || "") : (cond.step ? "Вопрос выключен" : "Выберите вопрос");
    const question = state.readOnly
      ? h("div", { class: "rr-card__title", text: label })
      : h("button", {
        type: "button",
        class: "rr-create",
        onClick: () => openQuestions(g, c),
      }, h("span", { text: label }), icon("chevron-down"));
    anchors.set(key, question);
    const errText = errors.get(key) || "";
    const err = h("p", {
      class: "rr-error",
      "data-rr-error": key,
      "aria-live": "polite",
      hidden: errText ? null : true,
      text: errText,
    });
    return h("div", { class: missing ? "rr-cond rr-warn" : "rr-cond" },
      h("div", { class: "rr-block-head" },
        h("span", { class: "rr-hint", text: "Вопрос" }),
        state.readOnly ? null : h("button", {
          type: "button",
          class: "btn ghost",
          "aria-label": `Убрать условие ${c + 1}`,
          onClick: () => dropCondition(g, c),
        }, icon("x")),
      ),
      question,
      missing ? h("p", { text: `Этот вопрос в ${state.cityLabel} выключен` }) : null,
      operatorControl(cond, spec, g, c),
      valueControl(cond, spec, g, c),
      err,
    );
  }

  function openQuestions(g, c) {
    const steps = (state.catalog.steps || []);
    const host = h("div");
    const search = h("input", {
      class: "input rr-input",
      type: "search",
      placeholder: "Найти вопрос",
    });
    const list = h("div");
    function draw() {
      if (!steps.length) {
        list.replaceChildren(h("p", {
          text: `В ${state.cityLabel} нет включённых вопросов анкеты. Включите нужные вопросы в «⚙️ Настройки», потом вернитесь сюда.`,
        }));
        return;
      }
      const grouped = new Map();
      for (const row of searchFilter(steps.map((step) => ({
        label: step.label || "",
        help: step.group_label || "",
        value: step,
      })), search.value || "")) {
        const name = row.item.value.group_label || "";
        if (!grouped.has(name)) grouped.set(name, []);
        grouped.get(name).push(row);
      }
      const nodes = [];
      for (const [name, rows] of grouped) {
        if (name) nodes.push(h("div", { class: "rr-eyebrow", text: name }));
        for (const row of rows) {
          nodes.push(h("button", {
            type: "button",
            class: "rr-check",
            onClick: () => chooseStep(g, c, row.item.value),
          }, ...highlightMatch(h, row.item.label || "", row.ranges.label)));
        }
      }
      list.replaceChildren(...nodes);
    }
    search.addEventListener("input", draw);
    host.append(search, list, h("p", { class: "rr-hint", text: "Показаны только включённые вопросы" }));
    draw();
    let sheet = null;
    sheet = openSheet(h, {
      title: "Вопрос",
      body: host,
      actions: [{ text: "Закрыть", kind: "ghost", onClick: () => sheet.close() }],
    });
    state.questionSheet = sheet;
  }

  function chooseStep(g, c, spec) {
    if (state.questionSheet) state.questionSheet.close();
    state.questionSheet = null;
    const ops = spec.operators || [];
    const op = ops.length === 1 ? ops[0].op : "";
    patch(g, c, { step: spec.step, op, values: [] }, true);
  }

  function operatorControl(cond, spec, g, c) {
    const ops = (spec && spec.operators) || [];
    if (!spec || ops.length === 0) return null;
    if (ops.length === 1) return null;
    const current = ops.some((item) => item.op === cond.op) ? cond.op : "";
    if (state.readOnly) {
      const found = ops.find((item) => item.op === current);
      return found ? h("p", { text: found.label || "" }) : null;
    }
    if (ops.length > 4) {
      const select = h("select", { class: "input rr-input" },
        h("option", { value: "", text: "Выберите, как сравнить" }),
        ...ops.map((item) => h("option", { value: item.op, text: item.label || "" })),
      );
      select.value = current;
      select.addEventListener("change", () => patch(g, c, { op: select.value, values: [] }, true));
      return h("label", { class: "rr-field" }, h("span", { class: "rr-hint", text: "Как сравнить" }), select);
    }
    return h("div", { class: "rr-field" },
      h("span", { class: "rr-hint", text: "Как сравнить" }),
      h("div", { class: "rr-seg", role: "radiogroup" },
        ...ops.map((item) => h("button", {
          type: "button",
          role: "radio",
          "aria-checked": item.op === current ? "true" : "false",
          text: item.label || "",
          onClick: () => patch(g, c, { op: item.op, values: [] }, true),
        })),
      ),
    );
  }

  function valueControl(cond, spec, g, c) {
    if (!spec || !takesValues(cond.op)) return null;
    if (spec.category === "text") {
      return h("p", { class: "rr-hint", text: "Текст ответа не сравнивается — проверяем только, заполнено ли поле" });
    }
    if (spec.category === "file") return null;
    if (spec.category === "select" || spec.category === "multi") return choiceControl(cond, spec, g, c);
    if (isDateOp(spec, cond.op)) return dateControl(cond, g, c);
    if (isIntOp(spec, cond.op)) return intControl(cond, spec, g, c);
    return buildV2Control(h, { degraded_kind: "text", kind: "text" }, filled(cond.values)[0] || "", (value) => {
      patch(g, c, { values: [String(value || "")] }, false);
    }, {}).control;
  }

  function choiceControl(cond, spec, g, c) {
    const options = spec.options || [];
    const selected = new Set(filled(cond.values));
    const single = cond.op === "eq" || (spec.operators || []).some((item) => item.op === cond.op && item.label === "равно");
    function setSelected(value, on) {
      const next = new Set(selected);
      if (single) {
        next.clear();
        if (on) next.add(value);
      } else if (on) next.add(value);
      else next.delete(value);
      patch(g, c, { values: [...next] }, true);
    }
    if (state.readOnly) return h("p", { text: [...selected].join(", ") });
    if (options.length > 8) return searchableChoices(options, selected, setSelected);
    return h("div", { class: "rr-chips" }, ...options.map((option) => {
      const on = selected.has(option);
      return h("button", {
        type: "button",
        class: on ? "rr-chip is-on" : "rr-chip",
        role: "checkbox",
        "aria-checked": on ? "true" : "false",
        text: option,
        onClick: () => setSelected(option, !on),
      });
    }));
  }

  function searchableChoices(options, selected, setSelected) {
    const host = h("div", { class: "rr-field" });
    const chips = h("div", { class: "rr-chips" });
    for (const value of selected) {
      chips.append(h("button", {
        type: "button",
        class: "rr-chip is-on",
        "aria-label": `Убрать значение «${value}»`,
        onClick: () => setSelected(value, false),
      }, h("span", { text: value }), icon("x")));
    }
    const search = h("input", { class: "input rr-input", type: "search", placeholder: "Найти значение" });
    const list = h("div");
    function draw() {
      const query = (search.value || "").trim().toLowerCase();
      list.replaceChildren(...options.filter((option) => !query || option.toLowerCase().includes(query)).map((option) => {
        const on = selected.has(option);
        return h("button", {
          type: "button",
          class: "rr-check",
          role: "checkbox",
          "aria-checked": on ? "true" : "false",
          onClick: () => setSelected(option, !on),
        }, h("span", { text: option }));
      }));
    }
    search.addEventListener("input", draw);
    draw();
    host.append(chips, search, list);
    return host;
  }

  function intControl(cond, spec, g, c) {
    const unit = spec.category === "birth_date" ? "лет" : (spec.unit || "");
    const values = cond.values || [];
    const fields = cond.op === "between" ? [0, 1] : [0];
    const inputs = fields.map((index) => numberInput(values[index] || "", (value) => {
      const next = [...(state.groups[g][c].values || [])];
      next[index] = value;
      patch(g, c, { values: next }, false);
    }));
    const row = h("div", { class: "rr-inline" }, ...inputs, unit ? h("span", { class: "rr-unit", text: unit }) : null);
    const hint = h("p", { class: "rr-hint", text: "Целое число, например 18" });
    const forum = spec.category === "birth_date" ? forumNote() : null;
    if (inputs[0]) anchors.set(`${g}:${c}`, inputs[0]);
    return h("div", { class: "rr-field" }, h("span", { class: "rr-hint", text: "Значение" }), row, hint, forum);
  }

  function numberInput(value, onValue) {
    if (state.readOnly) return h("span", { text: value });
    const input = h("input", { class: "input rr-input", type: "number", inputmode: "numeric" });
    input.value = value;
    input.addEventListener("input", () => onValue(input.value));
    return input;
  }

  function dateControl(cond, g, c) {
    const stored = filled(cond.values)[0] || "";
    if (state.readOnly) {
      return h("p", { text: stored });
    }
    const input = h("input", { class: "input rr-input", type: "date" });
    input.value = dmyToIso(stored);
    input.addEventListener("input", () => {
      patch(g, c, { values: input.value ? [isoToDmy(input.value)] : [] }, false);
    });
    anchors.set(`${g}:${c}`, input);
    return h("div", { class: "rr-field" },
      h("span", { class: "rr-hint", text: "Значение" }),
      input,
      h("p", { class: "rr-hint", text: "Например: 31.12.2026" }),
    );
  }

  function forumNote() {
    if (state.forumDate) {
      return h("p", { class: "rr-hint", text: `Считается на дату форума города: ${state.forumDate}` });
    }
    return h("div", { class: "rr-warn" }, h("p", {
      text: `У ${state.cityLabel} не указана дата форума — условие по возрасту не сработает. Укажите дату в настройках города.`,
    }));
  }

  const seeded = normalized(state.groups);
  state.groups = seeded.groups;
  if (seeded.changed && !state.readOnly) state.onChange(clone(state.groups));
  paint();
  return { el, update, focusFirstError };
}
