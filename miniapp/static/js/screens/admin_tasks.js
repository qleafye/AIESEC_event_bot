// Задания менеджера (экран 6 скетча Phase 16 в вебе, editorial-минимал 19.1-06): сегментированный
// переключатель «Активные | Архив» (одна поверхность, активный сегмент — заливка --accent,
// площадь тапа var(--tap-min)) — два состояния, список перерисовывается без перезагрузки; плоские
// строки (D-11, ui.js::flatRow) — номер, название, мета «категория · до дд.мм · N сдач», справа —
// монеты с иконкой coin. Архивная строка — слово «архив» в мете и иконка archive вместо coin.
// «Показать ещё», «Новое задание» -> #/task-edit/new, тап по строке -> #/task-edit/{id}.
// Подписи категорий приходят с сервера готовыми (RU) — фронт кодов не знает.

import { flatRow, emptyState } from "../ui.js";
import { icon } from "../icons.js";

const PAGE = 25;

function taskRow(h, navigate, item) {
  const metaParts = [item.category_label, `до ${item.deadline_short}`, `${item.pending + item.approved} сдач`];
  if (item.archived) metaParts.push("архив");
  const badges = [];
  if (item.pending > 0) badges.push(h("span", { class: "chip warn", text: `на проверке: ${item.pending}` }));
  if (item.overdue && !item.archived) badges.push(h("span", { class: "chip", text: "срок вышел" }));
  return flatRow(h, {
    leadText: `№${item.number}`,
    title: item.title,
    meta: metaParts.join(" · "),
    extra: badges.length ? h("div", { class: "task-foot" }, badges) : null,
    trailing: h("span", { class: "row-coins" }, icon(item.archived ? "archive" : "coin"), h("span", { text: String(item.coins) })),
    onClick: () => navigate(`#/task-edit/${item.id}`),
    cls: item.archived ? "admin-task-row archived" : "admin-task-row",
  });
}

export async function render(root, params, ctx) {
  const { h, api, navigate, me } = ctx;

  let archived = false;
  let offset = 0;
  let total = 0;
  let loading = false;

  const activeBtn = h("button", { class: "btn toggle-btn active", type: "button", text: "Активные" });
  const archiveBtn = h("button", { class: "btn toggle-btn", type: "button", text: "Архив" });
  const toggle = h("div", { class: "toggle", role: "tablist" }, activeBtn, archiveBtn);
  const list = h("div", { class: "flat-list" });
  const foot = h("div", { class: "list-foot" });
  const newBtn = h("button", {
    class: "btn", type: "button",
    onClick: () => navigate("#/task-edit/new"),
  }, icon("check"), h("span", { text: " Новое задание" }));

  root.append(h("h1", { text: "Задания" }), toggle, newBtn, list, foot);

  function paintToggle(page) {
    activeBtn.textContent = `Активные (${page.active_count})`;
    archiveBtn.textContent = `Архив (${page.archived_count})`;
    activeBtn.classList.toggle("active", !archived);
    archiveBtn.classList.toggle("active", archived);
    activeBtn.setAttribute("aria-selected", String(!archived));
    archiveBtn.setAttribute("aria-selected", String(archived));
  }

  async function load() {
    if (loading) return;
    loading = true;
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    let page;
    try {
      page = await api(`/admin/tasks?archived=${archived ? 1 : 0}&offset=${offset}&limit=${PAGE}`);
    } catch (err) {
      loading = false;
      if (!(err && (err.status === 401 || err.status === 403 || err.status === 503))) {
        foot.replaceChildren(h("p", { class: "error-inline", text: "Не удалось загрузить задания — попробуйте ещё раз." }));
      }
      return;
    }
    loading = false;
    total = page.total;
    paintToggle(page);
    for (const item of page.items) list.append(taskRow(h, navigate, item));
    offset += page.items.length;
    foot.replaceChildren();
    if (total === 0) {
      list.replaceChildren(emptyState(h, { me, text: page.empty_text || "" }));
      return;
    }
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: load,
      }));
    } else {
      foot.append(h("p", { class: "faint center", text: archived ? `В архиве: ${total}` : `Активных заданий: ${total}` }));
    }
  }

  // Переключение без перезагрузки: сброс страницы и новый запрос с другим флагом.
  function switchTo(toArchive) {
    if (archived === toArchive || loading) return;
    archived = toArchive;
    offset = 0;
    total = 0;
    list.replaceChildren();
    load();
  }
  activeBtn.addEventListener("click", () => switchTo(false));
  archiveBtn.addEventListener("click", () => switchTo(true));

  await load();
}
