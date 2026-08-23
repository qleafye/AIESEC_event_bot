// Карточка задания (editorial-минимал, D-11 hero-исключение — карточка с обложкой остаётся
// карточкой): обложка во всю ширину сверху (если есть photo_file_id; деградирует молча при
// ошибке загрузки), заголовок Heading-роли, мета «категория · монеты · дедлайн», чипы типов
// доказательства с иконками (image/file-text/link/pen-line), просроченный срок — иконкой +
// словом, строка статуса, «Нужно прислать», описание. MainButton «Сдать» -> #/submit/{id}.

import { icon } from "../icons.js";

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
    h("p", { class: "label-role", text: [task.category_label, `${task.coins} монет`, `до ${task.deadline_short}`].join(" · ") }),
  );
  const chips = proofChips(h, task.proof_type);
  if (chips) card.append(chips);
  if (task.overdue && task.overdue_hint) {
    card.append(h("p", { class: "flat-row-warn" }, icon("alert-triangle"), h("span", { text: ` ${task.overdue_hint}` })));
  }
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
