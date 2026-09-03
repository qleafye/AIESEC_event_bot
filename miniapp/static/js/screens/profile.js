// Профиль делегата (D-08: просмотр; D-24, план 21-11: правка) — плита с монограммой и именем
// (макет mockups/04-profile.png, план 23.1-05), чипы статусов (заявка + оплата — оплата
// только при непустом payment_status_label, D-08), два раздела плоских строк («Контакты» и
// «Анкета»), заметка о приватности; кнопка «✏️ Изменить анкету» — навигация на #/form внутри
// приложения (мастер точечной правки, не deep-link в бота).

import { icon } from "../icons.js";
import { flatRow, sectionTitle, labelText } from "../ui.js";

// Соответствие «ключ вопроса анкеты -> иконка строки контактов» — модульный словарь, а не
// строковое угадывание на каждый рендер; ключ вне словаря -> строка без иконки, не падаем.
const CONTACT_ICON = {
  reg_q_email: "mail",
  reg_q_phone: "smartphone",
  reg_q_work: "briefcase",
};

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton } = ctx;
  const me = await api("/profile");

  const chips = [];
  if (me.status_label) {
    chips.push(h("span", { class: "chip status ok" }, icon("check"), h("span", { text: me.status_label })));
  }
  // D-08: чип оплаты рисуется ТОЛЬКО при непустой подписи — сервер шлёт "" при выключенном
  // модуле оплаты; макет рисует «Оплачено» безусловно, это не спецификация.
  if (me.payment_status_label) {
    chips.push(h("span", { class: "chip pay ok" }, icon("check"), h("span", { text: me.payment_status_label })));
  }

  const personSub = [me.username, me.city_label].filter(Boolean).join(" · ");
  const plate = h("section", { class: "plate plate--profile" },
    h("div", { class: "plate-person" },
      me.initials ? h("span", { class: "plate-mono", text: me.initials }) : null,
      h("div", {},
        h("h1", { text: me.full_name || "" }),
        personSub ? h("p", { class: "plate-sub", text: personSub }) : null,
      ),
    ),
    chips.length ? h("hr", { class: "plate-rule" }) : null,
    chips.length ? h("div", { class: "plate-chips" }, ...chips) : null,
  );

  const sections = [];

  if (me.contacts.length) {
    sections.push(sectionTitle(h, me.contacts_eyebrow));
    sections.push(h("div", { class: "flat-list flush" },
      ...me.contacts.map((item) => flatRow(h, {
        icon: CONTACT_ICON[item.key],
        // D-04: строка уже с Lucide-иконкой слева — labelText (ui.js) снимает ведущий эмодзи
        // подписи REG_LABELS, не дублируя его рядом с иконкой (план 23.1-07).
        title: labelText(item.label),
        value: item.value,
        valueCls: "strong",
      })),
    ));
  }

  sections.push(sectionTitle(h, me.form_eyebrow));
  const formRows = [];
  if (me.form_progress_text) {
    formRows.push(flatRow(h, {
      title: me.form_progress_text,
      meta: me.form_meta_text,
      trailing: me.form_percent != null ? h("span", { class: "flat-row-num", text: `${me.form_percent}%` }) : null,
    }));
  }
  if (me.fields.length) {
    for (const f of me.fields) {
      formRows.push(flatRow(h, { title: f.label, value: f.value, valueCls: "strong" }));
    }
  } else if (!me.form_progress_text) {
    formRows.push(h("div", { class: "empty", text: "Анкета пока пустая." }));
  }
  sections.push(h("div", { class: "flat-list flush" }, ...formRows));

  const note = me.privacy_note
    ? h("p", { class: "profile-note" }, icon("lock"), h("span", { text: me.privacy_note }))
    : null;

  const actions = h("div", { class: "actions" });
  if (me.can_edit) {
    const go = () => navigate("#/form");
    actions.append(h("button", {
      class: "btn secondary", type: "button", onClick: go,
    }, icon("pen-line"), h("span", { text: me.edit_cta_text || "" })));
    setMainButton(me.edit_cta_text || null, go);
  } else {
    setMainButton(null);
  }

  root.append(plate, ...sections, note, actions);
}

export function unmount() {
  // MainButton снимает ядро при смене маршрута; здесь нечего чистить.
}
