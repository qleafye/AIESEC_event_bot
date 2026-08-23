// «Монеты» (зеркало экрана «🪙 Мои монеты» бота): крупный баланс (Display-роль, tabular-nums,
// иконка coin), докручиваемый через motion.js::countUp (D-17), история операций плоскими
// строками (дата+причина слева, дельта справа) постранично с «Показать ещё» (D-07). Подписи
// источников приходят из API (реестр).

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

  const balanceValue = h("span", { class: "stat-value", text: "0" });
  root.append(
    h("h1", { text: "Монеты" }),
    h("div", { class: "stats" },
      h("div", { class: "card stat" },
        h("div", { class: "balance-big" }, icon("coin"), balanceValue),
        h("div", { class: "faint", text: "баланс" }),
      ),
      h("div", { class: "card stat clickable", onClick: () => navigate("#/leaderboard") },
        h("div", { class: "stat-value", text: bal.rank == null ? "—" : String(bal.rank) }),
        h("div", { class: "faint", text: bal.participants ? `место из ${bal.participants}` : "место" }),
      ),
    ),
  );
  countUp(balanceValue, 0, bal.balance || 0);

  root.append(sectionTitle(h, "История"));
  const list = h("div", { class: "flat-list" });
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
