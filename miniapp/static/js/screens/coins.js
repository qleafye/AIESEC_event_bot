// «Монеты» (зеркало экрана «🪙 Мои монеты» бота): баланс, место в рейтинге, история операций
// постранично с «Показать ещё» (D-07). Подписи источников приходят из API (реестр).

const PAGE = 25;

function shortDate(value) {
  // "YYYY-MM-DD HH:MM:SS" -> "DD.MM" как в боте; что-то другое — как есть.
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  return m ? `${m[3]}.${m[2]}` : (value || "—");
}

function entryRow(h, item) {
  const delta = item.delta >= 0 ? `+${item.delta}` : String(item.delta);
  return h("div", { class: "row" },
    h("div", {},
      h("div", { text: item.reason || item.source_label }),
      h("div", { class: "faint", text: shortDate(item.created_at) }),
    ),
    h("span", { class: `delta ${item.delta >= 0 ? "plus" : "minus"}`, text: delta }),
  );
}

export async function render(root, params, ctx) {
  const { h, api, navigate } = ctx;
  const bal = await api("/coins/balance");

  root.append(
    h("h1", { text: "Монеты" }),
    h("div", { class: "stats" },
      h("div", { class: "card stat" },
        h("div", { class: "stat-value", text: String(bal.balance) }),
        h("div", { class: "faint", text: "баланс" }),
      ),
      h("div", { class: "card stat clickable", onClick: () => navigate("#/leaderboard") },
        h("div", { class: "stat-value", text: bal.rank == null ? "—" : String(bal.rank) }),
        h("div", { class: "faint", text: bal.participants ? `место из ${bal.participants}` : "место" }),
      ),
    ),
    h("h2", { text: "История" }),
  );

  const list = h("div", { class: "card list-card" });
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
      list.append(h("div", { class: "empty", text: page.empty_text || "" }));
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
