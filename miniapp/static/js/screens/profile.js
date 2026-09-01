// Профиль делегата — только просмотр (D-08): строки «подпись — значение» по REG_LABELS (подпись
// Label-роли, значение Body-роли) на общей плоской поверхности, статус заявки и оплаты, кнопка
// «Изменить — в боте» (deep-link на перерегистрацию через Telegram.WebApp.openTelegramLink; вне
// Telegram — обычная ссылка).

import { icon } from "../icons.js";

export async function render(root, params, ctx) {
  const { h, api, tg } = ctx;
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
  if (me.edit_deeplink) {
    const open = (e) => {
      if (tg && typeof tg.openTelegramLink === "function") {
        e.preventDefault();
        tg.openTelegramLink(me.edit_deeplink);
      }
    };
    actions.append(h("a", { class: "btn secondary", href: me.edit_deeplink, onClick: open },
      icon("pen-line"), h("span", { text: "Изменить — в боте" }),
    ));
    if (me.edit_hint) actions.append(h("p", { class: "faint center", text: me.edit_hint }));
  }

  root.append(head, fields, actions);
}
