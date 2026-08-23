// Экран «Задания» (зеркало экрана 1 скетча Phase 16): карточная сетка со статус-маркером,
// названием, монетами и дедлайном; «Показать ещё» — веб-нативная пагинация (D-07).
// Тап по карточке -> #/task/{id}. Все значения из API — только через textContent (h()).

const PAGE = 25;

// Подписи статусов — те же слова, что в списке бота (handlers/user_actions.py).
const STATUS = {
  new: { label: "новое", cls: "new" },
  pending: { label: "на проверке", cls: "pending" },
  approved: { label: "принято", cls: "approved" },
  rejected: { label: "отклонено", cls: "rejected" },
};

function statusChip(h, item) {
  const meta = STATUS[item.status] || STATUS.new;
  let text = meta.label;
  if (item.status === "rejected" && item.attempt) {
    text += item.limit ? ` (попытка ${item.attempt} из ${item.limit})` : ` (попытка ${item.attempt})`;
  }
  if (item.status === "approved" && item.coins_awarded != null) text += ` +${item.coins_awarded}`;
  return h("span", { class: `chip status ${meta.cls}`, text });
}

function taskCard(h, navigate, item) {
  const meta = [item.category_label, `${item.coins} монет`, `до ${item.deadline_short}`].join(" · ");
  return h("article", {
    class: `card task clickable ${item.overdue ? "overdue" : ""}`,
    role: "link",
    tabindex: "0",
    onClick: () => navigate(`#/task/${item.id}`),
    onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") navigate(`#/task/${item.id}`); },
  },
    h("div", { class: "task-head" },
      h("span", { class: `status-dot ${item.status}` }),
      h("div", { class: "title", text: item.title }),
    ),
    h("div", { class: "muted meta", text: meta }),
    h("div", { class: "task-foot" },
      statusChip(h, item),
      item.overdue ? h("span", { class: "chip warn", text: "срок вышел" }) : null,
    ),
  );
}

export async function render(root, params, ctx) {
  const { h, api, navigate } = ctx;
  const grid = h("div", { class: "task-grid" });
  const foot = h("div", { class: "list-foot" });
  root.append(h("h1", { text: "Задания" }), grid, foot);

  let offset = 0;
  let total = 0;

  async function load() {
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    const page = await api(`/tasks?offset=${offset}&limit=${PAGE}`);
    total = page.total;
    for (const item of page.items) grid.append(taskCard(h, navigate, item));
    offset += page.items.length;
    foot.replaceChildren();
    if (total === 0) {
      foot.append(h("div", { class: "empty", text: page.empty_text || "" }));
      return;
    }
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: load,
      }));
    } else {
      foot.append(h("p", { class: "faint center", text: `Всего заданий: ${total}` }));
    }
  }

  await load();
}
