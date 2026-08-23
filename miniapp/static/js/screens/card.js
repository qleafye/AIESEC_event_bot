// Карточка задания (зеркало карточки бота: game_labels.render_task_card_text) — обложка
// (если есть photo_file_id; /app/api/file/{id} появится в плане 19-04, до него картинка
// молча прячется по onerror), заголовок, категория · монеты · дедлайн, подсказка про
// просроченный срок, строка статуса, «Нужно прислать», описание. MainButton «Сдать» ->
// #/submit/{id} (экран сдачи — план 19-04; до него ядро покажет «Раздел пока недоступен»).

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton } = ctx;
  const task = await api(`/tasks/${encodeURIComponent(params.id)}`);

  const card = h("article", { class: "card task-card" });
  if (task.photo_file_id) {
    const img = h("img", {
      class: "cover",
      alt: "",
      src: `/app/api/file/${encodeURIComponent(task.photo_file_id)}`,
    });
    img.addEventListener("error", () => img.remove());
    card.append(img);
  }
  card.append(
    h("h1", { text: task.title }),
    h("p", { class: "muted", text: [task.category_label, `${task.coins} монет`, `до ${task.deadline_short}`].join(" · ") }),
  );
  if (task.overdue && task.overdue_hint) card.append(h("p", { class: "chip warn", text: task.overdue_hint }));
  card.append(
    h("div", { class: "row" },
      h("span", { class: "muted", text: "Статус" }),
      h("span", { class: `chip status ${task.status}`, text: task.status_line }),
    ),
    h("div", { class: "row" },
      h("span", { class: "muted", text: "Нужно прислать" }),
      h("span", { text: task.proof_hint }),
    ),
    h("div", { class: "pre", text: task.text }),
  );
  root.append(card);

  if (task.can_submit) {
    const go = () => navigate(`#/submit/${task.id}`);
    setMainButton("Сдать", go);
    root.append(h("button", { class: "btn", type: "button", text: "Сдать", onClick: go }));
  } else {
    setMainButton(null);
  }
}

export function unmount() {
  // MainButton снимает ядро при смене маршрута; здесь нечего чистить.
}
