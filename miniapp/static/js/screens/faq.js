// Экран #/faq — делегатский список «вопрос → ответ» (quick 260906-8uq, задача 5). Форма —
// свёрточная половина screens/questions.js: без пагинации (пунктов десятки, не сотни),
// раскрытие ответа по нажатию на строку (chevron-right/chevron-down), пустое состояние —
// текст из реестра (`faq_empty_text`, тот же ключ, что читает бот), никогда не пустой экран.
// Правило видимости (город делегата, перекрытие общего пункта городским) — на сервере
// (`services/faq.py` через `miniapp/routers/faq.py`), клиент только рисует готовый список.

import { flatRow, emptyState, labelText } from "../ui.js";
import { icon } from "../icons.js";

// Та же форма, что screens/questions.js::sectionLabel — подпись раздела из реестра
// (`body.dataset.sectionLabels`, miniapp/routers/page.py::section_labels), не литерал.
function sectionLabel(section) {
  try {
    const labels = JSON.parse(document.body.dataset.sectionLabels || "{}");
    return labels[section] || "";
  } catch (_) {
    return "";
  }
}

function isAuthError(err) {
  return Boolean(err && (err.status === 401 || err.status === 403 || err.status === 503));
}

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;

  let items = [];
  let emptyText = "";
  let openId = null;

  const list = h("div", { class: "flat-list" });
  root.append(h("h1", { text: labelText(sectionLabel("faq")) }), list);

  function faqRow(item) {
    const isOpen = openId === item.id;
    return flatRow(h, {
      title: item.question,
      extra: isOpen ? h("div", { class: "flat-row-meta", text: item.answer }) : null,
      trailing: icon(isOpen ? "chevron-down" : "chevron-right"),
      onClick: () => { openId = isOpen ? null : item.id; renderList(); },
    });
  }

  function renderList() {
    list.replaceChildren();
    if (items.length === 0) {
      list.replaceChildren(emptyState(h, { me, text: emptyText }));
      return;
    }
    for (const item of items) list.append(faqRow(item));
  }

  try {
    const page = await api("/faq");
    items = page.items || [];
    emptyText = page.empty_text || "";
  } catch (err) {
    if (!isAuthError(err)) {
      list.append(h("p", { class: "error-inline", text: "Не удалось загрузить список — попробуйте ещё раз." }));
    }
    return;
  }
  renderList();
}
