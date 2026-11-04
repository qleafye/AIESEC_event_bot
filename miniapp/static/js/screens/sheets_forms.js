// Карточка «Таблица и формы»: вкладки и книга только чтением, три слота форм,
// вопросы делегаций и одно включение. Геймификация, запись на сессии и лист
// амбассадоров здесь не живут.

import { flatRow, isCoreHandledError } from "../ui.js";

const TABS = [
  ["main", "Обычная"],
  ["short", "Краткая"],
  ["party", "Вечеринка"],
  ["incomplete", "Незавершённые"],
];

const SLOTS = [
  ["registration", "Регистрация"],
  ["delegation", "Делегации"],
  ["ambassador", "Амбассадоры"],
];

const QUESTIONS = [
  ["fullname", "Имя"],
  ["university", "Вуз"],
  ["course", "Курс"],
];

const TAB_EMPTY = "Не задана";
const SLOT_EMPTY = "Форма не выбрана";
const SLOT_EMPTY_BODY = "Нажмите «Выбрать форму» и возьмите одну из уже подключённых. Если списка нет, в чате откройте «Внешние формы».";
const NO_FORMS = "Подключённых форм пока нет. В чате откройте «Внешние формы» и подключите форму там.";
const TABLE_NOTE = "Имена вкладок и саму книгу отсюда не меняют. Это делается в чате.";
const KEYS_MISSING = "Сначала выберите вопросы формы: имя, вуз и курс. Без этого делегации не включаются.";
const DISARM_TEXT = "Новые анкеты делегаций больше не пройдут эту проверку. Уже поданные останутся.";
const ERROR_TEXT = "Не удалось открыть экран. Проверьте интернет и нажмите «Открыть экран снова».";
const RETRY = "Открыть экран снова";
const BOOK_ON = "Таблица подключена";
const BOOK_OFF = "Таблица не подключена";

function shown(value, fallback) {
  const text = typeof value === "string" ? value.trim() : "";
  return text || fallback;
}

function byKey(list, field, key) {
  if (!Array.isArray(list)) return null;
  return list.find((item) => item && item[field] === key) || null;
}

function armText(count) {
  return `Проверка пойдёт по выбранным вопросам. Бот одобрит ${count} человек.`;
}

function ask(h, text, agree, refuse, onAgree, onRefuse) {
  const box = h("div", { class: "confirm-box hidden" },
    h("p", { text }),
    h("button", {
      class: "btn",
      type: "button",
      text: agree,
      onClick: () => {
        box.close();
        onAgree();
      },
    }),
    h("button", {
      class: "btn ghost",
      type: "button",
      text: refuse,
      onClick: () => {
        box.close();
        if (onRefuse) onRefuse();
      },
    }),
  );
  box.open = () => box.classList.remove("hidden");
  box.close = () => box.classList.add("hidden");
  return box;
}

export async function render(root, params, ctx) {
  const { h, api } = ctx;
  let card = null;
  let notice = "";
  let openSlot = null;
  let openQuestion = null;
  let dialog = null;

  const fail = () => {
    root.replaceChildren(
      h("h1", { text: "Таблица и формы" }),
      h("p", { class: "thin-screen-state", text: ERROR_TEXT }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: RETRY,
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  async function reload() {
    const next = await api("/admin/sheets-forms");
    if (!next || typeof next !== "object") {
      fail();
      return;
    }
    card = next;
    notice = "";
    openSlot = null;
    openQuestion = null;
    dialog = null;
    paint();
  }

  function applyCard(next) {
    if (!next || typeof next !== "object" || !Array.isArray(next.tabs)) {
      fail();
      return;
    }
    card = next;
    notice = "";
    openSlot = null;
    openQuestion = null;
    dialog = null;
    paint();
  }

  async function saveSlot(slot, index) {
    try {
      applyCard(await api("/admin/sheets-forms/slot", {
        method: "POST",
        body: { slot, index },
      }));
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
    }
  }

  async function saveQuestion(which, index) {
    try {
      applyCard(await api("/admin/sheets-forms/question", {
        method: "POST",
        body: { which, index },
      }));
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
    }
  }

  async function turn(action) {
    dialog = null;
    try {
      const turned = await api("/admin/sheets-forms/delegations", {
        method: "POST",
        body: { action },
      });
      if (turned && turned.ok === false) {
        notice = turned.text || KEYS_MISSING;
        paint();
        return;
      }
      await reload();
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
    }
  }

  function openArm(count) {
    dialog = ask(
      h, armText(count), "Включить делегации", "Не включать",
      () => turn("on"),
      () => { dialog = null; },
    );
    paint();
  }

  async function arm() {
    let preview;
    try {
      preview = await api("/admin/sheets-forms/delegations", {
        method: "POST",
        body: { action: "preview" },
      });
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
      return;
    }
    if (!preview || preview.ok !== true || typeof preview.count !== "number") {
      notice = (preview && preview.text) || KEYS_MISSING;
      dialog = null;
      paint();
      return;
    }
    openArm(preview.count);
  }

  function disarm() {
    dialog = ask(
      h, DISARM_TEXT, "Выключить делегации", "Не выключать",
      () => turn("off"),
      () => { dialog = null; },
    );
    paint();
  }

  function paint() {
    const data = card || {};
    const titles = Array.isArray(data.forms) ? data.forms : [];
    const hasForms = titles.length > 0;
    const choices = Array.isArray(data.question_choices) ? data.question_choices : [];
    const ready = data.questions_ready === true;
    const armed = data.delegations_on === true;

    const tabRows = TABS.map(([key, title]) => {
      const row = byKey(data.tabs, "key", key);
      return flatRow(h, { title, value: shown(row && row.name, TAB_EMPTY) });
    });
    tabRows.push(flatRow(h, {
      title: "Книга",
      value: data.book_connected === true ? BOOK_ON : BOOK_OFF,
    }));

    const formRows = [];
    for (const [slot, title] of SLOTS) {
      const row = byKey(data.slots, "slot", slot);
      const chosen = row && typeof row.title === "string" ? row.title.trim() : "";
      formRows.push(flatRow(h, {
        title,
        meta: chosen || SLOT_EMPTY,
        extra: chosen ? null : h("p", { class: "label-role", text: SLOT_EMPTY_BODY }),
      }));
      if (hasForms) {
        formRows.push(h("button", {
          class: "btn",
          type: "button",
          text: "Выбрать форму",
          onClick: () => {
            openSlot = openSlot === slot ? null : slot;
            openQuestion = null;
            dialog = null;
            paint();
          },
        }));
      }
      if (openSlot === slot) {
        titles.forEach((label, index) => {
          formRows.push(h("button", {
            class: "btn",
            type: "button",
            text: shown(label, "Форма"),
            onClick: () => saveSlot(slot, index),
          }));
        });
      }
      if (slot !== "delegation") continue;
      for (const [which, label] of QUESTIONS) {
        const picked = byKey(data.questions, "which", which);
        const text = picked && typeof picked.text === "string" ? picked.text.trim() : "";
        formRows.push(flatRow(h, {
          title: label,
          meta: text || "не выбран",
          onClick: choices.length ? () => {
            openQuestion = openQuestion === which ? null : which;
            openSlot = null;
            dialog = null;
            paint();
          } : undefined,
        }));
        if (openQuestion !== which) continue;
        choices.forEach((labelText, index) => {
          formRows.push(h("button", {
            class: "btn secondary",
            type: "button",
            text: shown(labelText, "Вопрос"),
            onClick: () => saveQuestion(which, index),
          }));
        });
      }
      if (!ready) {
        formRows.push(h("p", { class: "label-role", text: notice || KEYS_MISSING }));
      } else if (armed) {
        if (notice) formRows.push(h("p", { class: "label-role", text: notice }));
        formRows.push(h("button", {
          class: "btn secondary",
          type: "button",
          text: "Выключить делегации",
          onClick: disarm,
        }));
      } else {
        if (notice) formRows.push(h("p", { class: "label-role", text: notice }));
        formRows.push(h("button", {
          class: "btn",
          type: "button",
          text: "Включить делегации",
          onClick: arm,
        }));
      }
    }
    if (!hasForms) formRows.push(h("p", { class: "label-role", text: NO_FORMS }));

    const nodes = [
      h("h1", { text: "Таблица и формы" }),
      h("div", { class: "flat-list" }, ...tabRows),
      h("p", { class: "label-role", text: TABLE_NOTE }),
      h("div", { class: "flat-list" }, ...formRows),
    ];
    if (dialog) nodes.push(dialog);
    root.replaceChildren(...nodes);
    if (dialog) dialog.open();
  }

  root.replaceChildren(h("h1", { text: "Таблица и формы" }));
  try {
    card = await api("/admin/sheets-forms");
  } catch (err) {
    if (isCoreHandledError(err)) return;
    fail();
    return;
  }
  if (!card || typeof card !== "object") {
    fail();
    return;
  }
  paint();
}
