// Экран #/questions — журнал вопросов делегатов (quick 260904-2cj). Форма — журнальная
// половина screens/admin_coins.js: постраничный список, emptyState на пустом журнале,
// ошибки сервера текстом из payload, а не кодом ответа. Правило статуса — на сервере
// (services/questions.py через miniapp/routers/questions.py), клиент только показывает
// готовые подписи (status_label/filters[].label/answer_button/sent_toast/empty_text) —
// доменные тексты в JS не хардкодятся.

import { flatRow, emptyState, labelText } from "../ui.js";
import { icon } from "../icons.js";

const PAGE = 20;

function errorText(err, fallback) {
  if (err && err.payload && err.payload.text) return err.payload.text;
  return fallback;
}

function isAuthError(err) {
  return Boolean(err && (err.status === 401 || err.status === 403 || err.status === 503));
}

// Та же форма, что screens/applications.js::sectionLabel — подпись раздела из реестра
// (`body.dataset.sectionLabels`, miniapp/routers/page.py::section_labels), не литерал.
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

  let status = null; // null == чип «Все»
  let offset = 0;
  let total = 0;
  let items = [];
  let openId = null;
  let emptyText = "";
  let answerButtonLabel = "";
  let sentToast = "";
  let answerPlaceholder = "";
  let answerToggleLabel = "";
  // Quick 260906-8uq (FAQ-06): «В FAQ» — отдельное открытое состояние (мутуально исключимо
  // с answerForm по данным: can_add_to_faq истинно ТОЛЬКО для отвеченных строк, can_answer —
  // только для НЕ отвеченных), но своя переменная понятнее, чем перегрузка openId.
  let openFaqId = null;
  let toFaqButtonLabel = "";
  let toFaqSavedToast = "";
  const drafts = {};
  const formErrors = {};
  const faqDrafts = {};
  const faqErrors = {};
  let noticeTimer = null;

  const notice = h("p", { class: "chip success hidden" });
  const filtersRow = h("div", { class: "chip-row" });
  const list = h("div", { class: "flat-list" });
  const foot = h("div", { class: "list-foot" });

  root.append(
    h("h1", { text: labelText(sectionLabel("questions")) }),
    notice,
    filtersRow,
    list,
    foot,
  );

  function say(text) {
    if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
    notice.textContent = text || "";
    notice.className = `chip success${text ? "" : " hidden"}`;
    if (text) noticeTimer = setTimeout(() => say(""), 3000);
  }

  function renderFilters(filters) {
    filtersRow.replaceChildren();
    const activeKey = status || "all";
    for (const f of filters || []) {
      filtersRow.append(h("button", {
        class: `chip appl-filter-chip${f.key === activeKey ? " on" : ""}`,
        type: "button",
        text: `${f.label} · ${f.count}`,
        onClick: () => {
          if (f.key === activeKey) return;
          status = f.key === "all" ? null : f.key;
          offset = 0;
          items = [];
          openId = null;
          load();
        },
      }));
    }
  }

  function answerForm(item) {
    const textarea = h("textarea", { class: "input", rows: "3", placeholder: answerPlaceholder });
    textarea.value = drafts[item.id] || "";
    textarea.addEventListener("input", () => { drafts[item.id] = textarea.value; });
    // Quick 260904-kk6 (Q2): ошибка предыдущей попытки переживает перерисовку списка (без
    // этого патч статуса res.item в ветке ok:false стирал бы сообщение об ошибке вместе со
    // старой формой).
    const existingError = formErrors[item.id];
    const errLine = h("p", { class: `error-inline${existingError ? "" : " hidden"}`, text: existingError || "" });
    const submitBtn = h("button", { class: "btn", type: "button", text: answerButtonLabel || "" });

    async function submit() {
      const text = textarea.value;
      submitBtn.disabled = true;
      errLine.classList.add("hidden");
      errLine.textContent = "";
      try {
        const res = await api(`/questions/${item.id}/answer`, { method: "POST", body: { text } });
        if (res.ok) {
          delete drafts[item.id];
          delete formErrors[item.id];
          openId = null;
          const idx = items.findIndex((row) => row.id === item.id);
          if (idx >= 0) {
            // Quick 260906-52m (D-06): подпись статуса — с сервера (res.item), не литералом
            // тут. answer_text остаётся локальным — _status_patch его намеренно не несёт
            // (менеджер только что его набрал). Если res.item не пришёл (старый сервер за
            // кэшем), спред undefined безопасен — строка остаётся как была.
            items[idx] = { ...items[idx], ...res.item, answer_text: text };
          }
          say(sentToast);
          renderList();
          return;
        }
        // Поле НЕ очищаем: менеджер не должен перенабирать ответ после отказа сервера.
        // Форма остаётся открытой (openId не трогаем) — черновик и ошибка подхватятся из
        // drafts/formErrors при следующей перерисовке.
        const message = res.text || errorText({ payload: res }, "Не получилось отправить.");
        formErrors[item.id] = message;
        const idx = items.findIndex((row) => row.id === item.id);
        if (idx >= 0 && res.item) {
          items[idx] = { ...items[idx], ...res.item };
        }
        renderList();
      } catch (err) {
        submitBtn.disabled = false;
        if (!isAuthError(err)) {
          const message = errorText(err, "Не получилось отправить — попробуйте ещё раз.");
          formErrors[item.id] = message;
          errLine.textContent = message;
          errLine.classList.remove("hidden");
        }
      }
    }

    submitBtn.addEventListener("click", submit);
    return h("div", { class: "task-actions" }, textarea, errLine, submitBtn);
  }

  // Quick 260906-8uq (FAQ-06): форма «В FAQ» — два предзаполненных поля (вопрос делегата,
  // ответ менеджера), можно поправить перед сохранением. Дубль (сервер отвечает
  // {ok:false, reason:"already"}) — текст ошибки из реестра не заведён отдельным ключом
  // (это редкий предохранитель, не основной путь), фиксированная человеческая строка ниже.
  function toFaqForm(item) {
    const qInput = h("textarea", { class: "input", rows: "2" });
    qInput.value = faqDrafts[item.id] ? faqDrafts[item.id].q : (item.question_text || "");
    const aInput = h("textarea", { class: "input", rows: "3" });
    aInput.value = faqDrafts[item.id] ? faqDrafts[item.id].a : (item.answer_text || "");
    qInput.addEventListener("input", () => {
      faqDrafts[item.id] = { q: qInput.value, a: aInput.value };
    });
    aInput.addEventListener("input", () => {
      faqDrafts[item.id] = { q: qInput.value, a: aInput.value };
    });

    const existingError = faqErrors[item.id];
    const errLine = h("p", { class: `error-inline${existingError ? "" : " hidden"}`, text: existingError || "" });
    const saveBtn = h("button", { class: "btn", type: "button", text: toFaqButtonLabel || "" });

    async function submit() {
      const question = qInput.value;
      const answer = aInput.value;
      saveBtn.disabled = true;
      errLine.classList.add("hidden");
      errLine.textContent = "";
      try {
        const res = await api("/faq", { method: "POST", body: { question, answer } });
        if (res.ok) {
          delete faqDrafts[item.id];
          delete faqErrors[item.id];
          openFaqId = null;
          say(toFaqSavedToast);
          renderList();
          return;
        }
        const message = "Такой вопрос уже в FAQ.";
        faqErrors[item.id] = message;
        renderList();
      } catch (err) {
        saveBtn.disabled = false;
        if (!isAuthError(err)) {
          const message = errorText(err, "Не получилось сохранить — попробуйте ещё раз.");
          faqErrors[item.id] = message;
          errLine.textContent = message;
          errLine.classList.remove("hidden");
        }
      }
    }

    saveBtn.addEventListener("click", submit);
    return h("div", { class: "task-actions" }, qInput, aInput, errLine, saveBtn);
  }

  function questionRow(item) {
    const isOpen = openId === item.id;
    const isFaqOpen = openFaqId === item.id;
    const extraChildren = [
      h("div", { class: "flat-row-meta", text: item.question_text || "" }),
    ];
    if (item.answer_text) {
      extraChildren.push(h("div", { class: "flat-row-meta muted", text: `↩️ ${item.answer_text}` }));
    }
    if (isOpen) extraChildren.push(answerForm(item));
    if (isFaqOpen) extraChildren.push(toFaqForm(item));

    // aria-label/title: иконка без текста иначе читается скринридером как «кнопка».
    const toggle = item.can_answer
      ? h("button", {
        class: "btn ghost", type: "button",
        "aria-label": answerToggleLabel || "", title: answerToggleLabel || "",
        onClick: () => { openId = isOpen ? null : item.id; renderList(); },
      }, icon(isOpen ? "chevron-down" : "pen-line"))
      : null;

    const faqToggle = item.can_add_to_faq
      ? h("button", {
        class: "btn ghost", type: "button",
        "aria-label": toFaqButtonLabel || "", title: toFaqButtonLabel || "",
        onClick: () => { openFaqId = isFaqOpen ? null : item.id; renderList(); },
      }, icon("help-circle"))
      : null;

    const trailingChildren = [toggle, faqToggle].filter(Boolean);

    const meta = [item.name || item.username || "—", item.asked_at, item.stuck ? "залип" : null]
      .filter(Boolean).join(" · ");

    return flatRow(h, {
      title: `#${item.id} · ${item.status_label}`,
      meta,
      extra: h("div", {}, ...extraChildren),
      trailing: trailingChildren.length ? trailingChildren : null,
    });
  }

  function renderList() {
    list.replaceChildren();
    if (items.length === 0) {
      list.replaceChildren(emptyState(h, { me, text: emptyText }));
      return;
    }
    for (const item of items) list.append(questionRow(item));
  }

  async function load() {
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    let page;
    try {
      const q = `offset=${offset}&limit=${PAGE}` + (status ? `&status=${status}` : "");
      page = await api(`/questions?${q}`);
    } catch (err) {
      foot.replaceChildren();
      renderList();
      if (!isAuthError(err)) foot.append(h("p", { class: "error-inline", text: "Не удалось загрузить журнал — попробуйте ещё раз." }));
      return;
    }
    total = page.total;
    emptyText = page.empty_text || "";
    answerButtonLabel = page.answer_button || "";
    sentToast = page.sent_toast || "";
    answerPlaceholder = page.answer_placeholder || "";
    answerToggleLabel = page.answer_toggle_label || "";
    toFaqButtonLabel = page.to_faq_button || "";
    toFaqSavedToast = page.to_faq_saved_toast || "";
    renderFilters(page.filters);
    items = items.concat(page.items);
    offset += page.items.length;
    renderList();
    foot.replaceChildren();
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: load,
      }));
    }
  }

  await load();
}
