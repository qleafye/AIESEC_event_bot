// «Рейтинг» (зеркало экрана 4 скетча Phase 16): топ-50 + закреплённая строка «ты N-й».
// Имена — те же, что публикует бот; ничего лишнего API не отдаёт.

function row(h, item) {
  return h("div", { class: `row rank-row ${item.is_me ? "me" : ""}` },
    h("span", { class: "rank", text: `${item.rank}.` }),
    h("span", { class: "name", text: item.name }),
    h("span", { class: "balance", text: String(item.balance) }),
  );
}

export async function render(root, params, ctx) {
  const { h, api } = ctx;
  const board = await api("/leaderboard?limit=50");

  root.append(h("h1", { text: "Рейтинг" }));
  const list = h("div", { class: "card list-card" });
  if (!board.items.length) {
    list.append(h("div", { class: "empty", text: board.empty_text || "" }));
  }
  for (const item of board.items) list.append(row(h, item));
  root.append(list);

  const me = board.me || {};
  const place = me.rank == null ? "Ты пока не в рейтинге" : `Ты ${me.rank}-й из ${board.total}`;
  root.append(h("div", { class: "card pinned" },
    h("span", { class: "name", text: place }),
    h("span", { class: "balance", text: String(me.balance == null ? 0 : me.balance) }),
  ));
}
