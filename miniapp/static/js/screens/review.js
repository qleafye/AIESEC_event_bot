// Очередь проверки сдач (экран 5 скетча Phase 16 в вебе, D-07, editorial-минимал 19.1-06):
// ровно одна карточка — делегат · город, задание, попытка, отметки «после дедлайна» /
// «задание в архиве», части (фото и документы — через /app/api/file/{id}, текст и ссылки —
// цитатой), счётчик «Осталось: N». Подвал действий по вердикту nav.html: главное действие
// («Принять · +N») — MainButton Telegram; вторичные («Своя сумма», «Отклонить», «Пропустить»)
// — ряд из трёх обведённых кнопок, «Отклонить» — цветом --danger-text и иконкой x.
//
// «Пропустить» — чисто клиентское: следующий запрос с offset+1, сервер не трогаем.
// После решения — сразу следующая карточка с тем же offset (решённая выпала из очереди).
// Ответ {ok:false, reason:"already"} — «Уже обработано» спокойным текстом, не ошибкой, и
// тоже переходим дальше: кто-то уже решил, менеджеру делать нечего.
//
// Конфетти (D-17) на этом экране сознательно НЕ запускается: у менеджера принятие сдачи —
// рутина проверки, а не личное достижение делегата (сужение D-17, план 19.1-06). Хаптик
// успеха остаётся — это обратная связь по нажатию, не праздничный momentum.

import { icon } from "../icons.js";
import { emptyState } from "../ui.js";
import { haptic } from "../motion.js";

const PART_ICON = { photo: "image", document: "file-text", text: "pen-line", link: "link" };

export async function render(root, params, ctx) {
  const { h, api, setMainButton, me } = ctx;

  let offset = 0;
  let busy = false;
  const notice = h("p", { class: "chip accent hidden" });
  const holder = h("div");
  root.append(h("h1", { text: "Проверка сдач" }), notice, holder);

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "accent"}${text ? "" : " hidden"}`;
  }

  function fileUrl(fileId) {
    return `/app/api/file/${encodeURIComponent(fileId)}`;
  }

  function partNode(part) {
    const iconName = PART_ICON[part.kind] || "file-text";
    if (part.kind === "photo") {
      const img = h("img", { class: "review-photo", alt: "", src: fileUrl(part.content) });
      img.addEventListener("error", () => {
        img.replaceWith(h("p", { class: "faint" }, icon("image"), h("span", { text: " Фото не загрузилось — откройте в боте" })));
      });
      return h("div", { class: "review-part" }, img, part.caption ? h("p", { class: "muted", text: part.caption }) : null);
    }
    if (part.kind === "document") {
      return h("div", { class: "review-part" },
        h("a", { class: "btn ghost", href: fileUrl(part.content), target: "_blank", rel: "noopener" }, icon(iconName), h("span", { text: " Открыть файл" })),
        part.caption ? h("p", { class: "muted", text: part.caption }) : null,
      );
    }
    if (part.kind === "link") {
      return h("div", { class: "review-part flat-row-icon-line" },
        icon(iconName),
        h("a", { href: part.content, target: "_blank", rel: "noopener", text: part.content }),
      );
    }
    return h("blockquote", { class: "review-quote pre", text: part.content || "—" });
  }

  async function load() {
    holder.replaceChildren(h("p", { class: "loading", text: "Загружаем…" }));
    setMainButton(null);
    let card;
    try {
      card = await api(`/review/next?offset=${encodeURIComponent(offset)}`);
    } catch (err) {
      if (!(err && (err.status === 401 || err.status === 403 || err.status === 503))) {
        holder.replaceChildren(h("p", { class: "error-inline", text: "Не удалось загрузить очередь — попробуйте ещё раз." }));
      }
      return;
    }
    if (card.empty) {
      const allSkipped = card.remaining > 0;
      // Нет отдельного ключа реестра под пустую очередь проверки (план 19.1-02 его не завёл) —
      // используем прежний литеральный текст, как договорено в интерфейсе плана 19.1-06.
      holder.replaceChildren(
        emptyState(h, {
          me,
          text: allSkipped ? `Пропущено всё — осталось ${card.remaining}.` : "Сдач на проверке нет.",
          action: allSkipped
            ? h("button", { class: "btn secondary", type: "button", onClick: () => { offset = 0; load(); } }, icon("rotate-ccw"), h("span", { text: " Начать сначала" }))
            : null,
        }),
      );
      return;
    }
    draw(card);
  }

  function draw(card) {
    const sid = card.submission.id;
    const delegateMeta = [card.delegate.name || "—", card.delegate.username ? `@${card.delegate.username}` : null, card.delegate.city]
      .filter(Boolean).join(" · ");

    const flags = [];
    if (card.attempt) flags.push(h("span", { class: "chip", text: `Попытка ${card.attempt.k} из ${card.attempt.n}` }));
    if (card.after_deadline) flags.push(h("span", { class: "chip warn" }, icon("clock"), h("span", { text: " Сдано после дедлайна — решение за вами" })));
    if (card.archived_task) flags.push(h("span", { class: "chip" }, icon("archive"), h("span", { text: " Задание в архиве — сдачу всё равно нужно решить" })));

    const coinsInput = h("input", { class: "input", type: "number", min: "1", step: "1", inputmode: "numeric" });
    coinsInput.value = String(card.task.coins);
    const customBox = h("div", { class: "field hidden" },
      h("label", { text: "Сколько монет начислить?" }),
      coinsInput,
      h("button", { class: "btn", type: "button", text: "Начислить", onClick: () => decide("approve", { coins: Number(coinsInput.value) }) }),
    );

    const reasonInput = h("textarea", { class: "input", rows: "2", placeholder: "Причина — делегат увидит её в сообщении" });
    const rejectBox = h("div", { class: "field hidden" },
      h("label", { text: "Причина отклонения" }),
      reasonInput,
      h("button", { class: "btn danger", type: "button", text: "Отклонить", onClick: () => decide("reject", { reason: reasonInput.value.trim() }) }),
    );

    function toggle(box) {
      const wasHidden = box.classList.contains("hidden");
      customBox.classList.add("hidden");
      rejectBox.classList.add("hidden");
      if (wasHidden) box.classList.remove("hidden");
    }

    async function decide(action, body) {
      if (busy) return;
      if (action === "reject" && !body.reason) { say("Напишите причину — делегат увидит её в сообщении.", "warn"); return; }
      if (action === "approve" && body && !(Number.isInteger(body.coins) && body.coins > 0)) { say("Сумма — целое число больше нуля, например 15.", "warn"); return; }
      busy = true;
      setMainButton(`Принять · +${card.task.coins}`, approveDefault, { disabled: true });
      try {
        // Два литеральных пути (не `${action}`): сторожевой тест сверяет их с маршрутами.
        const res = action === "approve"
          ? await api(`/review/${encodeURIComponent(sid)}/approve`, { method: "POST", body: body || {} })
          : await api(`/review/${encodeURIComponent(sid)}/reject`, { method: "POST", body });
        if (res.ok) {
          say(action === "approve" ? `Одобрено, +${res.coins}` : "Сдача отклонена", action === "approve" ? "success" : "danger");
          haptic("success");
        } else {
          say("Уже обработано — кто-то успел раньше.", "accent");
        }
        await load();
      } catch (err) {
        if (err && err.status === 400 && err.payload && err.payload.text) say(err.payload.text, "warn");
        else if (err && err.status === 403 && err.payload && err.payload.text) say(err.payload.text, "warn");
        else if (err && err.status === 404) { say("Сдача не найдена — переходим дальше.", "accent"); await load(); }
        else if (!(err && (err.status === 401 || err.status === 403 || err.status === 503))) say("Не получилось — попробуйте ещё раз.", "warn");
      } finally {
        busy = false;
      }
    }

    const approveDefault = () => decide("approve", null);

    const rejectBtn = h("button", { class: "btn ghost review-btn-danger", type: "button", onClick: () => toggle(rejectBox) }, icon("x"), h("span", { text: " Отклонить" }));
    const customBtn = h("button", { class: "btn ghost", type: "button", text: "Своя сумма", onClick: () => toggle(customBox) });
    const skipBtn = h("button", { class: "btn ghost review-btn-muted", type: "button", text: "Пропустить", onClick: () => { offset += 1; say(""); load(); } });

    holder.replaceChildren(
      h("div", { class: "review-head label-role" },
        h("span", { text: `Осталось: ${card.remaining}` }),
        h("span", { text: ` · сдача ${card.position} из ${card.remaining}` }),
      ),
      h("article", { class: "card review-card" },
        h("p", { class: "flat-row-meta", text: delegateMeta }),
        h("h2", { text: card.task.title }),
        h("p", { class: "flat-row-meta", text: [card.task.category_label, `${card.task.coins} монет`, card.task.proof_label ? `нужно: ${card.task.proof_label}` : null].filter(Boolean).join(" · ") }),
        flags.length ? h("div", { class: "task-foot" }, flags) : null,
        h("div", { class: "review-parts" }, card.parts.length ? card.parts.map(partNode) : h("p", { class: "muted", text: "Содержимое: —" })),
      ),
      h("div", { class: "review-actions" },
        h("div", { class: "review-actions-row" }, customBtn, rejectBtn, skipBtn),
        customBox,
        rejectBox,
      ),
    );
    setMainButton(`Принять · +${card.task.coins}`, approveDefault);
  }

  await load();
}

export function unmount() {
  // MainButton снимает ядро при смене маршрута; offset пропусков живёт только на экране.
}
