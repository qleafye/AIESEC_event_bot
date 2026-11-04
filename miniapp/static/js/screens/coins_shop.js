// Папка «Баллы и магазин»: две строки, без витрины. «Баллы» — только при праве геймы.

import { flatRow } from "../ui.js";

export async function render(root, params, ctx) {
  const { h, navigate, me } = ctx;
  const caps = me && Array.isArray(me.caps) ? me.caps : [];
  const rows = [];
  if (caps.includes("moderate_game")) {
    rows.push({ icon: "coins", title: "Баллы", hash: "#/admin-coins" });
  }
  rows.push({
    icon: "wallet",
    title: "Стойка магазина",
    hash: "#/scanner",
    meta: "Отметить выдачу.",
  });
  root.replaceChildren();
  if (!rows.length) {
    root.append(
      h("h1", { text: "В этом режиме пусто" }),
      h("p", { text: "Для вашей роли здесь нет строк. Если дело пропало, попросите того, кто выдаёт доступ, проверить права." }),
    );
    return;
  }
  root.append(
    h("div", { class: "flat-list" }, rows.map((row) => flatRow(h, {
      icon: row.icon,
      title: row.title,
      meta: row.meta,
      chevron: true,
      onClick: () => navigate(row.hash),
    }))),
    h("p", { class: "label-role list-foot", text: "Товары и витрина остаются в чате." }),
  );
}
