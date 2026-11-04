// Папка «День форума»: плоский список входов. Сценариев дня и редактора программы нет.

import { flatRow } from "../ui.js";

export async function render(root, params, ctx) {
  const { h, navigate } = ctx;
  const rows = [
    { icon: "camera", title: "Сканер QR", hash: "#/scanner" },
    { icon: "calendar", title: "Расписание", hash: "#/program" },
    { icon: "check-circle-2", title: "Отметки", hash: "#/arrivals" },
    { icon: "alert-triangle", title: "SOS", hash: "#/chat/sos", meta: "Пока в чате" },
    { icon: "briefcase", title: "Находки", hash: "#/chat/lost", meta: "Пока в чате" },
  ];
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
    h("p", {
      class: "label-role list-foot",
      text: "Сборка дня без программиста появится отдельно. Здесь только входы.",
    }),
  );
}
