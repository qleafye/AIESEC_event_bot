// «Монеты» (зеркало экрана «🪙 Мои монеты» бота): плита с балансом (план 23.1-06 — та же
// система, что и остальные семь делегатских экранов), крупный баланс (Display-роль,
// tabular-nums, иконка coin), докручиваемый через motion.js::countUp (D-17), чип-переход на
// рейтинг справа, история операций плоскими строками (дата+причина слева, дельта справа)
// постранично с «Показать ещё» (D-07). Подписи источников приходят из API (реестр).

import { flatRow, sectionTitle, emptyState } from "../ui.js";
import { icon } from "../icons.js";
import { countUp } from "../motion.js";

const PAGE = 25;

function shortDate(value) {
  // "YYYY-MM-DD HH:MM:SS" -> "DD.MM" как в боте; что-то другое — как есть.
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  return m ? `${m[3]}.${m[2]}` : (value || "—");
}

function entryRow(h, item) {
  const delta = item.delta >= 0 ? `+${item.delta}` : String(item.delta);
  return flatRow(h, {
    title: item.reason || item.source_label,
    meta: shortDate(item.created_at),
    trailing: h("span", { class: `delta ${item.delta >= 0 ? "plus" : "minus"}` }, delta),
  });
}

export async function render(root, params, ctx) {
  const { h, api, navigate, me } = ctx;
  const bal = await api("/coins/balance");

  let hub = {};
  try { hub = await api("/hub"); } catch (_) {
    // Пятый источник подписей не заводим (см. hub.js) — отказ ручки обвязки не роняет
    // экран, плита остаётся без надзаголовка/единицы.
  }

  const plateBig = h("span", { class: "plate-big", text: "0" });
  const rankChip = bal.rank == null ? null : h("button", {
    class: "chip sec plate-rank", type: "button",
    onClick: () => navigate("#/leaderboard"),
    text: bal.participants ? `${bal.rank}-й из ${bal.participants}` : `${bal.rank}-й`,
  });
  root.append(
    h("section", { class: "plate plate--list plate--coins" },
      h("div", { class: "plate-eyebrow", text: hub.balance_eyebrow || "" }),
      h("div", { class: "plate-row" },
        plateBig,
        h("span", { class: "plate-coin" }, icon("coin")),
        h("span", { class: "plate-sub", text: hub.balance_unit || "" }),
        rankChip,
      ),
    ),
  );
  countUp(plateBig, 0, bal.balance || 0);

  root.append(sectionTitle(h, "История"));
  const list = h("div", { class: "flat-list flush" });
  const foot = h("div", { class: "list-foot" });
  root.append(list, foot);

  let offset = 0;
  async function load() {
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    const page = await api(`/coins/history?offset=${offset}&limit=${PAGE}`);
    for (const item of page.items) list.append(entryRow(h, item));
    offset += page.items.length;
    foot.replaceChildren();
    if (page.total === 0) {
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
    if (offset < page.total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${page.total - offset})`,
        onClick: load,
      }));
    }
  }
  await load();
}
