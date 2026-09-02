// Профиль делегата (D-08: просмотр; D-24, план 21-11: правка) — строки «подпись — значение»
// по REG_LABELS (подпись Label-роли, значение Body-роли) на общей плоской поверхности, статус
// заявки и оплаты, кнопка «✏️ Изменить анкету» — навигация на #/form внутри приложения
// (мастер точечной правки, не deep-link в бота).

import { icon } from "../icons.js";

export async function render(root, params, ctx) {
  const { h, api, navigate } = ctx;
  const me = await api("/profile");

  const head = h("div", { class: "card" },
    h("h1", { text: me.full_name || "" }),
    me.username ? h("p", { class: "muted", text: me.username }) : null,
    h("div", { class: "row" },
      h("span", { class: "muted", text: "Заявка" }),
      h("span", { class: `chip status ${me.status}`, text: me.status_label }),
    ),
    // Пустая подпись = модуль оплаты выключен (сервер шлёт "" при payment_enabled=off):
    // строки «Оплата» на профиле нет вовсе, а не вечное «Не оплатил».
    me.payment_status_label ? h("div", { class: "row" },
      h("span", { class: "muted", text: "Оплата" }),
      h("span", { class: `chip pay ${me.payment_status}`, text: me.payment_status_label }),
    ) : null,
  );

  const fields = h("div", { class: "flat-list" });
  if (!me.fields.length) {
    fields.replaceChildren(h("div", { class: "empty", text: "Анкета пока пустая." }));
  }
  for (const f of me.fields) {
    fields.append(h("div", { class: "field-row" },
      h("div", { class: "label-role", text: f.label }),
      h("div", { class: "pre", text: f.value }),
    ));
  }

  const actions = h("div", { class: "actions" });
  if (me.can_edit) {
    actions.append(h("button", {
      class: "btn secondary", type: "button", onClick: () => navigate("#/form"),
    }, icon("pen-line"), h("span", { text: me.edit_cta_text || "" })));
  }

  root.append(head, fields, actions);
}
