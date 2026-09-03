// Карточка задания (план 23.1-05, макет mockups/05-task.png): плита с наградой и остатком
// срока сверху (обложка — первым ребёнком плиты, если есть photo_file_id; деградирует молча
// при ошибке загрузки), «Что сделать», крупный блок «Нужно прислать» с чипами типов
// доказательства, строки фактов (статус/проверка). MainButton «Сдать» -> #/submit/{id}.

import { icon } from "../icons.js";
import { flatRow, sectionTitle } from "../ui.js";

const PROOF_ICON = { photo: "image", pdf: "file-text", text: "pen-line", link: "link" };
const PROOF_ORDER = ["photo", "pdf", "text", "link"];

function proofChips(h, raw) {
  if (!raw) return null;
  const codes = new Set(String(raw).split(",").map((s) => s.trim()).filter(Boolean));
  const present = PROOF_ORDER.filter((code) => codes.has(code));
  if (!present.length) return null;
  return h("div", { class: "proof-chips" },
    ...present.map((code) => h("span", { class: "chip proof" }, icon(PROOF_ICON[code]))),
  );
}

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton } = ctx;
  const task = await api(`/tasks/${encodeURIComponent(params.id)}`);

  const plate = h("article", { class: "plate plate--task" });
  if (task.photo_file_id) {
    const img = h("img", {
      class: "cover",
      alt: "",
      src: `/app/api/file/${encodeURIComponent(task.photo_file_id)}`,
    });
    img.addEventListener("error", () => img.remove());
    plate.append(img);
  }
  plate.append(
    h("p", { class: "plate-eyebrow", text: [task.category_label, `до ${task.deadline_short}`].join(" · ") }),
    h("h1", { text: task.title }),
    h("hr", { class: "plate-rule" }),
  );
  // Строка «сколько осталось» — тот же слот и у обычного, и у просроченного задания:
  // deadline_left_text (свежее) или overdue_hint (мягкий дедлайн вышел, D-05); пусто (оба
  // поля null) -> блока времени нет вовсе, награда остаётся одна в строке.
  const timeText = task.deadline_left_text || task.overdue_hint;
  plate.append(h("div", { class: "plate-row" },
    timeText ? icon("clock") : null,
    timeText ? h("span", { class: "plate-sub", text: timeText }) : null,
    h("div", { class: "plate-reward" }, h("b", { text: `+${task.coins}` }), icon("coin")),
  ));
  root.append(plate);

  root.append(
    sectionTitle(h, task.todo_eyebrow),
    h("p", { class: "pre muted", text: task.text }),
  );

  if (task.proof_hint) {
    root.append(
      sectionTitle(h, task.proof_eyebrow),
      h("div", { class: "proof-drop" },
        icon("camera"),
        h("div", { class: "proof-drop-title", text: task.proof_hint }),
        h("div", { class: "proof-drop-note", text: task.proof_note }),
        proofChips(h, task.proof_type),
      ),
    );
  }

  // Просроченное задание помечается alert-triangle + flat-row-warn прямо на строке статуса
  // (D-05: дедлайн мягкий, сдача разрешена — это визуальная пометка, а не запрет).
  root.append(h("div", { class: "flat-list flush" },
    flatRow(h, {
      icon: task.overdue ? "alert-triangle" : undefined,
      cls: task.overdue ? "flat-row-warn" : undefined,
      title: "Статус", value: task.status_line, valueCls: "strong",
    }),
    flatRow(h, { title: "Проверка", value: task.review_note, valueCls: "strong" }),
  ));

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
