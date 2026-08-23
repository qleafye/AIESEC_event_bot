// Экран «Задания» (editorial-минимал, D-11): плоские строки на одной поверхности — статус-
// точка, название, мета «категория · до {дата}», справа монеты с иконкой coin (tabular-nums).
// Просроченное задание — не только цветом, но и иконкой alert-triangle + словом. «Показать
// ещё» — веб-нативная пагинация (D-07). Тап по строке -> #/task/{id}. Все значения из API —
// только через textContent (h()/flatRow()).

import { flatRow, emptyState } from "../ui.js";
import { icon } from "../icons.js";

const PAGE = 25;

const STATUS_META = {
  pending: "на проверке",
  approved: "принято",
};

function metaLine(item) {
  if (item.status === "rejected") {
    if (item.attempt) {
      return item.limit
        ? `${item.category_label} · отклонено · попытка ${item.attempt} из ${item.limit}`
        : `${item.category_label} · отклонено · попытка ${item.attempt}`;
    }
    return `${item.category_label} · отклонено`;
  }
  const known = STATUS_META[item.status];
  if (known) return `${item.category_label} · ${known}`;
  return `${item.category_label} · до ${item.deadline_short}`;
}

function coinsTrailing(h, item) {
  const value = item.status === "approved" && item.coins_awarded != null ? item.coins_awarded : item.coins;
  return h("span", { class: "row-coins" }, String(value), icon("coin"));
}

function overdueBadge(h) {
  return h("span", { class: "flat-row-warn" }, icon("alert-triangle"), h("span", { text: " просрочено" }));
}

function taskRow(h, navigate, item) {
  return flatRow(h, {
    dot: item.status,
    title: item.title,
    meta: metaLine(item),
    extra: item.overdue ? overdueBadge(h) : null,
    trailing: coinsTrailing(h, item),
    onClick: () => navigate(`#/task/${item.id}`),
  });
}

export async function render(root, params, ctx) {
  const { h, api, navigate, me } = ctx;
  root.append(h("h1", { text: "Задания" }));
  const list = h("div", { class: "flat-list" });
  const foot = h("div", { class: "list-foot" });
  root.append(list, foot);

  let offset = 0;
  let total = 0;

  async function load() {
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    const page = await api(`/tasks?offset=${offset}&limit=${PAGE}`);
    total = page.total;
    for (const item of page.items) list.append(taskRow(h, navigate, item));
    offset += page.items.length;
    foot.replaceChildren();
    if (total === 0) {
      list.replaceChildren(emptyState(h, {
        me,
        text: page.empty_text || "",
        action: h("button", {
          class: "btn secondary", type: "button", text: "Обновить",
          onClick: () => { offset = 0; load(); },
        }),
      }));
      return;
    }
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: load,
      }));
    } else {
      foot.append(h("p", { class: "faint center", text: `Всего заданий: ${total}` }));
    }
  }

  await load();
}
