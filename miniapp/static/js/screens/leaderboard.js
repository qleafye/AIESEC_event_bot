// «Рейтинг» (D-19): подиум топ-3, плоские строки мест 4–10, закреплённая своя строка («ещё N
// монет до M-го места» либо факт лидерства) и обезличенный хвост «ещё K участников» — имена
// за пределами топ-10 в DOM не попадают (T-19.1-18). Топ-1 у самого зрителя — хаптик успеха
// (все уровни) и конфетти (только motion "full").

import { flatRow, emptyState } from "../ui.js";
import { icon } from "../icons.js";
import { confetti, haptic } from "../motion.js";

function placeClass(rank) {
  if (rank === 1) return "first";
  if (rank === 2) return "second";
  return "third";
}

function podiumPlace(h, item) {
  const cls = placeClass(item.rank);
  return h("div", { class: `podium-place ${cls}` },
    h("span", { class: "rank-badge", text: String(item.rank) }),
    cls === "first" ? icon("trophy") : null,
    h("div", { class: "name", text: item.name }),
    h("div", { class: "balance", text: String(item.balance) }),
  );
}

function podium(h, top) {
  const wrap = h("div", { class: "podium", "data-count": String(top.length) });
  for (const item of top) wrap.append(podiumPlace(h, item));
  return wrap;
}

function rankRow(h, item) {
  return flatRow(h, {
    leadText: `${item.rank}.`,
    title: item.name,
    trailing: h("span", { text: String(item.balance) }),
    cls: item.is_me ? "me" : "",
  });
}

function pinnedRow(h, board) {
  const me = board.me || {};
  const wrap = h("div", { class: "card pinned" });
  if (me.rank == null) {
    wrap.append(
      h("span", { class: "name", text: "Ты пока не в рейтинге" }),
      h("span", { class: "balance", text: String(me.balance == null ? 0 : me.balance) }),
    );
    return wrap;
  }
  const label = h("div", {}, h("div", { class: "name", text: `Ты ${me.rank}-й` }));
  if (me.rank === 1) {
    label.append(h("div", { class: "faint", text: "Лидер рейтинга" }));
  } else {
    const above = board.items.find((it) => it.rank === me.rank - 1);
    if (above) {
      const gap = Math.max(0, above.balance - (me.balance || 0));
      label.append(h("div", { class: "faint" },
        "ещё ",
        h("span", { class: "gap-amount", text: String(gap) }),
        ` монет до ${above.rank}-го места`,
      ));
    }
  }
  wrap.append(label, h("span", { class: "balance", text: String(me.balance) }));
  return wrap;
}

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;

  async function load() {
    root.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    const board = await api("/leaderboard?limit=50");
    root.replaceChildren(h("h1", { text: "Рейтинг" }));

    if (!board.items.length) {
      root.append(emptyState(h, {
        me,
        text: board.empty_text || "",
        action: h("button", { class: "btn secondary", type: "button", text: "Обновить", onClick: load }),
      }));
      return;
    }

    const top = board.items.slice(0, 3);
    const podiumEl = podium(h, top);
    root.append(podiumEl);

    const restRows = board.items.slice(3, 10);
    if (restRows.length) {
      const list = h("div", { class: "flat-list" });
      for (const item of restRows) list.append(rankRow(h, item));
      root.append(list);
    }

    const tailCount = Math.max(0, board.total - 10);
    if (tailCount > 0) {
      root.append(h("div", { class: "rank-tail", text: `ещё ${tailCount} участников` }));
    }

    root.append(pinnedRow(h, board));

    if (board.me && board.me.rank === 1) {
      haptic("success");
      const firstPlace = podiumEl.querySelector(".podium-place.first");
      confetti(firstPlace || podiumEl);
    }
  }

  await load();
}
