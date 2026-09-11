// Экран «Задания» (editorial-минимал, D-11): плита с общим числом активных заданий (план
// 23.1-06 — та же система, что и остальные семь делегатских экранов), ниже — плоские строки
// на полноширинной поверхности: статус-точка, название, мета «категория · до {дата}», справа
// монеты с иконкой coin (tabular-nums). Просроченное задание — не только цветом, но и иконкой
// alert-triangle + словом. «Показать ещё» — веб-нативная пагинация (D-07). Тап по строке ->
// #/task/{id}. Все значения из API — только через textContent (h()/flatRow()).

import { flatRow, emptyState, guardedRender } from "../ui.js";
import { icon } from "../icons.js";
import { countUp } from "../motion.js";

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

// Quick 260911-5ij (W2, Пилар 6): тело прежнего render() — вызывается ТОЛЬКО через
// guardedRender (см. export ниже), чтобы отказ первого api() красил состояние ошибки, а не
// оставлял белый экран.
async function draw(root, params, ctx) {
  const { h, api, navigate, me } = ctx;

  let hub = {};
  try { hub = await api("/hub"); } catch (_) {
    // Пятый источник подписей не заводим (см. hub.js) — отказ ручки обвязки не роняет
    // экран, плита остаётся без надзаголовка.
  }

  const plateBig = h("span", { class: "plate-big", text: "0" });
  root.append(
    h("section", { class: "plate plate--list plate--tasks" },
      h("div", { class: "plate-eyebrow", text: hub.tasks_eyebrow || "" }),
      h("div", { class: "plate-row" }, plateBig),
    ),
  );

  const list = h("div", { class: "flat-list flush" });
  const foot = h("div", { class: "list-foot" });
  root.append(list, foot);

  let offset = 0;
  let total = 0;
  let counted = false;

  async function load() {
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    const page = await api(`/tasks?offset=${offset}&limit=${PAGE}`);
    total = page.total;
    for (const item of page.items) list.append(taskRow(h, navigate, item));
    offset += page.items.length;
    foot.replaceChildren();
    if (counted) {
      plateBig.textContent = String(total);
    } else {
      counted = true;
      countUp(plateBig, 0, total);
    }
    if (total === 0) {
      list.replaceChildren(emptyState(h, {
        me,
        text: page.empty_text || "",
        action: h("button", {
          class: "btn secondary", type: "button", text: "Обновить",
          // Quick 260911-5ij (W2): та же обёртка, что «Показать ещё» ниже — цена
          // консистентности: повтор перерисовывает экран с нуля (offset сбрасывается), а не
          // тихо зовёт load() второй раз.
          onClick: () => guardedRender(root, ctx, () => draw(root, params, ctx)),
        }),
      }));
      return;
    }
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        // Quick 260911-5ij (W2): отказ на «Показать ещё» раньше был необработанным rejected
        // promise после завершения render() — тихий провал без текста и кнопки. Общая
        // обёртка ловит его, ценой перерисовки экрана с нуля (позиция пагинации теряется).
        onClick: () => guardedRender(root, ctx, () => draw(root, params, ctx)),
      }));
    } else {
      foot.append(h("p", { class: "faint center", text: `Всего заданий: ${total}` }));
    }
  }

  await load();
}

export async function render(root, params, ctx) {
  await guardedRender(root, ctx, () => draw(root, params, ctx));
}
