// Экран менеджера #/admin-faq (quick 260906-nxp) — ведение того же списка FAQ, что видит
// делегат на #/faq и бот в handlers/admin_faq.py: постраничный список (flatRow, D-11),
// форма добавления, раскрытая карточка пункта (правка вопроса/ответа, порядок стрелками,
// показать/скрыть, «мой город ↔ все города»), удаление с двухшаговым подтверждением. Все
// доменные тексты — status_text/toggle_label/city_toggle_label/city_hint/
// delete_confirm_text/empty_text/city_badge/bound_city_label — приходят с сервера
// (miniapp/routers/faq.py); в этом файле только служебные строки («Сохранить», «Отмена»,
// «Добавить», «Да, удалить», «Показать ещё», «Загрузка…», generic-ошибки сети).

import { flatRow, emptyState, labelText } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";

const PAGE = 25;
const RELOAD_LIMIT_MAX = 50; // server-side ADMIN_LIMIT_MAX — потолок «перезагрузи видимое»

function errorText(err, fallback) {
  if (err && err.payload && err.payload.text) return err.payload.text;
  return fallback;
}

function isAuthError(err) {
  return Boolean(err && (err.status === 401 || (err.status === 403 && err.reason !== "out_of_scope" && err.reason !== "not_found") || err.status === 503));
}

// Та же форма, что screens/questions.js::sectionLabel/screens/faq.js::sectionLabel — подпись
// раздела из реестра (body.dataset.sectionLabels), не литерал.
function sectionLabel(section) {
  try {
    const labels = JSON.parse(document.body.dataset.sectionLabels || "{}");
    return labels[section] || "";
  } catch (_) {
    return "";
  }
}

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;

  let offset = 0;
  let total = 0;
  let items = [];
  let loading = false;
  let noticeTimer = null;

  let emptyText = "";
  let cityChoice = false;
  let boundCityLabel = null;
  let cityHint = "";
  let questionMax = 300;
  let answerMax = 4000;

  let addOpen = false;
  let openId = null;   // раскрытая карточка пункта
  let openEdit = null; // { id, field: "question"|"answer" } — открытый редактор внутри карточки
  let openDelete = null; // id пункта, для которого показан confirm-box

  const addDrafts = { question: "", answer: "" };
  let addError = "";
  const editDrafts = {}; // `${id}:${field}` -> черновик текста, переживает перерисовку
  const editErrors = {}; // `${id}:${field}` -> ошибка сохранения поля

  const notice = h("p", { class: "chip success hidden" });
  const addBtn = h("button", { class: "btn", type: "button", text: "Добавить" });
  const addHolder = h("div");
  const list = h("div", { class: "flat-list" });
  const foot = h("div", { class: "list-foot" });

  root.append(
    h("h1", { text: labelText(sectionLabel("faq")) }),
    notice,
    addBtn,
    addHolder,
    list,
    foot,
  );

  function say(text) {
    if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
    notice.textContent = text || "";
    notice.className = `chip success${text ? "" : " hidden"}`;
    if (text) noticeTimer = setTimeout(() => say(""), 3000);
  }

  function applyPageMeta(page) {
    emptyText = page.empty_text || "";
    cityChoice = Boolean(page.city_choice);
    boundCityLabel = page.bound_city_label || null;
    cityHint = page.city_hint || "";
    questionMax = page.question_max || questionMax;
    answerMax = page.answer_max || answerMax;
  }

  function applyItemPatch(item) {
    if (!item) return;
    const idx = items.findIndex((row) => row.id === item.id);
    if (idx >= 0) items[idx] = item;
  }

  // ── форма добавления ──

  function renderAddForm() {
    if (!addOpen) {
      addHolder.replaceChildren();
      return;
    }
    const qInput = h("textarea", { class: "input", rows: "2", maxlength: String(questionMax) });
    qInput.value = addDrafts.question;
    qInput.addEventListener("input", () => { addDrafts.question = qInput.value; });
    const aInput = h("textarea", { class: "input", rows: "3", maxlength: String(answerMax) });
    aInput.value = addDrafts.answer;
    aInput.addEventListener("input", () => { addDrafts.answer = aInput.value; });

    const visText = cityChoice ? `Будет виден: только ${boundCityLabel}` : cityHint;
    const errLine = h("p", { class: `error-inline${addError ? "" : " hidden"}`, text: addError || "" });
    const saveBtn = h("button", { class: "btn", type: "button", text: "Сохранить" });
    const cancelBtn = h("button", {
      class: "btn ghost", type: "button", text: "Отмена",
      onClick: () => { addOpen = false; addError = ""; renderAddForm(); },
    });

    async function submit() {
      saveBtn.disabled = true;
      try {
        const res = await api("/admin/faq", {
          method: "POST",
          body: { question: addDrafts.question, answer: addDrafts.answer },
        });
        if (res.ok) {
          addOpen = false;
          addDrafts.question = "";
          addDrafts.answer = "";
          addError = "";
          renderAddForm();
          haptic("success");
          say("Добавлено");
          await reloadVisible();
          return;
        }
        // Дубль — форма и текст НЕ очищаются, менеджер не перенабирает.
        addError = "Такой вопрос уже в FAQ.";
        saveBtn.disabled = false;
        renderAddForm();
      } catch (err) {
        saveBtn.disabled = false;
        if (!isAuthError(err)) {
          addError = errorText(err, "Не получилось сохранить — попробуйте ещё раз.");
          renderAddForm();
        }
      }
    }
    saveBtn.addEventListener("click", submit);

    addHolder.replaceChildren(
      h("div", { class: "field" }, h("label", { text: "Вопрос" }), qInput),
      h("div", { class: "field" }, h("label", { text: "Ответ" }), aInput),
      h("p", { class: "flat-row-meta", text: visText }),
      errLine,
      h("div", { class: "task-actions" }, saveBtn, cancelBtn),
    );
  }

  addBtn.addEventListener("click", () => {
    addOpen = !addOpen;
    if (!addOpen) addError = "";
    renderAddForm();
  });

  // ── правка одного поля внутри карточки ──

  function editSection(item, field, label, currentText, maxLen) {
    const key = `${item.id}:${field}`;
    const isEditing = Boolean(openEdit && openEdit.id === item.id && openEdit.field === field);
    const btn = h("button", {
      class: "btn ghost", type: "button",
      onClick: () => {
        openEdit = isEditing ? null : { id: item.id, field };
        renderList();
      },
    }, icon("pen-line"), h("span", { text: ` ${label}` }));
    if (!isEditing) return btn;

    const textarea = h("textarea", { class: "input", rows: field === "question" ? "2" : "3", maxlength: String(maxLen) });
    textarea.value = editDrafts[key] != null ? editDrafts[key] : currentText;
    textarea.addEventListener("input", () => { editDrafts[key] = textarea.value; });
    const existingError = editErrors[key];
    const errLine = h("p", { class: `error-inline${existingError ? "" : " hidden"}`, text: existingError || "" });
    const saveBtn = h("button", { class: "btn", type: "button", text: "Сохранить" });

    async function submit() {
      saveBtn.disabled = true;
      try {
        const res = await api(`/admin/faq/${item.id}`, { method: "PATCH", body: { [field]: textarea.value } });
        delete editDrafts[key];
        delete editErrors[key];
        openEdit = null;
        applyItemPatch(res.item);
        renderList();
      } catch (err) {
        saveBtn.disabled = false;
        if (!isAuthError(err)) {
          editErrors[key] = errorText(err, "Не получилось сохранить — попробуйте ещё раз.");
          renderList();
        }
      }
    }
    saveBtn.addEventListener("click", submit);

    return h("div", {}, btn, textarea, errLine, saveBtn);
  }

  // ── стрелки / показать-скрыть / город / удаление ──

  async function move(item, direction) {
    if (loading) return;
    try {
      await api(`/admin/faq/${item.id}/move`, { method: "POST", body: { direction } });
      await reloadVisible();
    } catch (err) {
      if (!isAuthError(err)) say(errorText(err, "Не получилось переставить — попробуйте ещё раз."));
    }
  }

  async function toggleEnabled(item) {
    try {
      const res = await api(`/admin/faq/${item.id}`, { method: "PATCH", body: { enabled: !item.enabled } });
      applyItemPatch(res.item);
      renderList();
    } catch (err) {
      if (!isAuthError(err)) say(errorText(err, "Не получилось изменить — попробуйте ещё раз."));
    }
  }

  async function toggleCity(item) {
    const target = item.is_general ? "mine" : "all";
    try {
      const res = await api(`/admin/faq/${item.id}`, { method: "PATCH", body: { city: target } });
      applyItemPatch(res.item);
      renderList();
    } catch (err) {
      if (!isAuthError(err)) say(errorText(err, "Не получилось изменить город — попробуйте ещё раз."));
    }
  }

  function deleteConfirmBox(item) {
    let busy = false;

    async function confirmDelete() {
      if (busy) return;
      busy = true;
      try {
        await api(`/admin/faq/${item.id}`, { method: "DELETE" });
        openDelete = null;
        openId = null;
        haptic("success");
        say("Удалено");
        await reloadVisible();
      } catch (err) {
        busy = false;
        if (!isAuthError(err)) say(errorText(err, "Не получилось удалить — попробуйте ещё раз."));
      }
    }

    return h("div", { class: "confirm-box" },
      h("p", { text: item.delete_confirm_text }),
      h("button", { class: "btn", type: "button", text: "Да, удалить", onClick: confirmDelete }),
      h("button", {
        class: "btn ghost", type: "button", text: "Отмена",
        onClick: () => { openDelete = null; renderList(); },
      }),
    );
  }

  // ── строка / карточка списка ──

  function itemCard(item) {
    if (openDelete === item.id) {
      return h("div", {}, deleteConfirmBox(item));
    }
    const cityRow = item.city_toggle_label
      ? h("div", { class: "task-actions" },
          h("button", { class: "btn secondary", type: "button", text: item.city_toggle_label, onClick: () => toggleCity(item) }),
        )
      : h("p", { class: "flat-row-meta muted", text: cityHint });

    return h("div", {},
      h("div", { class: "flat-row-meta", text: item.answer }),
      h("div", { class: "task-actions" }, editSection(item, "question", "Вопрос", item.question, questionMax)),
      h("div", { class: "task-actions" }, editSection(item, "answer", "Ответ", item.answer, answerMax)),
      h("div", { class: "task-actions" },
        h("button", {
          class: "btn ghost", type: "button", "aria-label": "Выше", title: "Выше",
          disabled: !item.can_move_up, onClick: () => move(item, "up"),
        }, icon("chevron-up")),
        h("button", {
          class: "btn ghost", type: "button", "aria-label": "Ниже", title: "Ниже",
          disabled: !item.can_move_down, onClick: () => move(item, "down"),
        }, icon("chevron-down")),
      ),
      h("div", { class: "task-actions" },
        h("button", { class: "btn secondary", type: "button", text: item.toggle_label, onClick: () => toggleEnabled(item) }),
      ),
      cityRow,
      h("div", { class: "task-actions" },
        h("button", {
          class: "btn ghost", type: "button", text: "Удалить",
          onClick: () => { openDelete = item.id; renderList(); },
        }, icon("trash-2")),
      ),
    );
  }

  function itemRow(item) {
    const isOpen = openId === item.id;
    return flatRow(h, {
      leadText: `№${item.number}`,
      title: item.question,
      meta: `${item.city_badge} · ${item.status_text}`,
      extra: isOpen ? itemCard(item) : null,
      trailing: icon(isOpen ? "chevron-down" : "chevron-right"),
      onClick: () => {
        openId = isOpen ? null : item.id;
        openEdit = null;
        openDelete = null;
        renderList();
      },
    });
  }

  function renderList() {
    list.replaceChildren();
    if (items.length === 0) {
      list.replaceChildren(emptyState(h, { me, text: emptyText }));
      return;
    }
    for (const item of items) list.append(itemRow(item));
  }

  function renderFoot() {
    foot.replaceChildren();
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: loadMore,
      }));
    }
  }

  // ── загрузка страниц ──

  async function loadPage(pageOffset, pageLimit) {
    return api(`/admin/faq?offset=${pageOffset}&limit=${pageLimit}`);
  }

  async function loadInitial() {
    if (loading) return;
    loading = true;
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    let page;
    try {
      page = await loadPage(0, PAGE);
    } catch (err) {
      loading = false;
      foot.replaceChildren();
      if (!isAuthError(err)) foot.append(h("p", { class: "error-inline", text: "Не удалось загрузить список — попробуйте ещё раз." }));
      return;
    }
    loading = false;
    applyPageMeta(page);
    items = page.items;
    offset = page.items.length;
    total = page.total;
    renderList();
    renderFoot();
  }

  async function loadMore() {
    if (loading) return;
    loading = true;
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    let page;
    try {
      page = await loadPage(offset, PAGE);
    } catch (err) {
      loading = false;
      foot.replaceChildren();
      if (!isAuthError(err)) foot.append(h("p", { class: "error-inline", text: "Не удалось загрузить список — попробуйте ещё раз." }));
      return;
    }
    loading = false;
    applyPageMeta(page);
    items = items.concat(page.items);
    offset += page.items.length;
    total = page.total;
    renderList();
    renderFoot();
  }

  // После любой успешной мутации перезагружаем ровно то, что уже было видно (не постранично) —
  // номера и доступность стрелок пересчитывает сервер по свежему списку.
  async function reloadVisible() {
    const count = Math.min(Math.max(items.length, PAGE), RELOAD_LIMIT_MAX);
    const page = await loadPage(0, count);
    applyPageMeta(page);
    items = page.items;
    offset = page.items.length;
    total = page.total;
    renderList();
    renderFoot();
  }

  await loadInitial();
}
