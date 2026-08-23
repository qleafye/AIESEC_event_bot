// Очередь проверки сдач (экран 5 скетча Phase 16 в вебе, D-07): ровно одна карточка —
// делегат · город, задание, попытка, отметки «после дедлайна» / «задание в архиве», части
// (фото и документы — через /app/api/file/{id}, текст и ссылки — цитатой), счётчик
// «Осталось: N». Действия: «✅ Принять», «✏️ Своя сумма», «❌ Отклонить», «⏭ Пропустить».
//
// «Пропустить» — чисто клиентское: следующий запрос с offset+1, сервер не трогаем.
// После решения — сразу следующая карточка с тем же offset (решённая выпала из очереди).
// Ответ {ok:false, reason:"already"} — «Уже обработано» спокойным текстом, не ошибкой, и
// тоже переходим дальше: кто-то уже решил, менеджеру делать нечего.

const PART_ICON = { photo: "📸", document: "📄", text: "✍️", link: "🔗" };

export async function render(root, params, ctx) {
  const { h, api, setMainButton, tg } = ctx;

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
    const icon = PART_ICON[part.kind] || "📎";
    if (part.kind === "photo") {
      const img = h("img", { class: "review-photo", alt: "", src: fileUrl(part.content) });
      img.addEventListener("error", () => {
        img.replaceWith(h("p", { class: "faint", text: "📸 фото не загрузилось — откройте в боте" }));
      });
      return h("div", { class: "review-part" }, img, part.caption ? h("p", { class: "muted", text: part.caption }) : null);
    }
    if (part.kind === "document") {
      return h("div", { class: "review-part" },
        h("a", { class: "btn ghost", href: fileUrl(part.content), target: "_blank", rel: "noopener", text: `${icon} Открыть файл` }),
        part.caption ? h("p", { class: "muted", text: part.caption }) : null,
      );
    }
    if (part.kind === "link") {
      return h("div", { class: "review-part" },
        h("a", { href: part.content, target: "_blank", rel: "noopener", text: `${icon} ${part.content}` }),
      );
    }
    return h("blockquote", { class: "review-quote pre", text: `${icon} ${part.content || "—"}` });
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
      holder.replaceChildren(
        h("div", { class: "empty" },
          h("p", { text: allSkipped ? `Пропущено всё — осталось ${card.remaining}.` : "Сдач на проверке нет." }),
          allSkipped
            ? h("button", { class: "btn secondary", type: "button", text: "↩️ Начать сначала", onClick: () => { offset = 0; load(); } })
            : null,
        ),
      );
      return;
    }
    draw(card);
  }

  function draw(card) {
    const sid = card.submission.id;
    const delegate = [card.delegate.name || "—", card.delegate.username ? `@${card.delegate.username}` : null, card.delegate.city]
      .filter(Boolean).join(" · ");

    const flags = [];
    if (card.attempt) flags.push(h("span", { class: "chip", text: `🔁 Попытка ${card.attempt.k} из ${card.attempt.n}` }));
    if (card.after_deadline) flags.push(h("span", { class: "chip warn", text: "⏰ Сдано после дедлайна — решение за вами" }));
    if (card.archived_task) flags.push(h("span", { class: "chip", text: "🗄 Задание в архиве — сдачу всё равно нужно решить" }));

    const coinsInput = h("input", { class: "input", type: "number", min: "1", step: "1", inputmode: "numeric" });
    coinsInput.value = String(card.task.coins);
    const customBox = h("div", { class: "field hidden" },
      h("label", { text: "Сколько монет начислить?" }),
      coinsInput,
      h("button", { class: "btn", type: "button", text: "✅ Начислить", onClick: () => decide("approve", { coins: Number(coinsInput.value) }) }),
    );

    const reasonInput = h("textarea", { class: "input", rows: "2", placeholder: "Причина — делегат увидит её в сообщении" });
    const rejectBox = h("div", { class: "field hidden" },
      h("label", { text: "Причина отклонения" }),
      reasonInput,
      h("button", { class: "btn danger", type: "button", text: "❌ Отклонить", onClick: () => decide("reject", { reason: reasonInput.value.trim() }) }),
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
      setMainButton("✅ Принять", approveDefault, { disabled: true });
      try {
        // Два литеральных пути (не `${action}`): сторожевой тест сверяет их с маршрутами.
        const res = action === "approve"
          ? await api(`/review/${encodeURIComponent(sid)}/approve`, { method: "POST", body: body || {} })
          : await api(`/review/${encodeURIComponent(sid)}/reject`, { method: "POST", body });
        if (res.ok) {
          say(action === "approve" ? `✅ Одобрено, +${res.coins}🪙` : "❌ Сдача отклонена", action === "approve" ? "success" : "danger");
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
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

    holder.replaceChildren(
      h("div", { class: "review-head" },
        h("span", { class: "chip accent", text: `Осталось: ${card.remaining}` }),
        h("span", { class: "faint", text: `сдача ${card.position} из ${card.remaining}` }),
      ),
      h("article", { class: "card review-card" },
        h("p", { class: "title", text: `👤 ${delegate}` }),
        h("h2", { text: card.task.title }),
        h("p", { class: "muted", text: [card.task.category_label, `предложено ${card.task.coins}🪙`, card.task.proof_label ? `нужно: ${card.task.proof_label}` : null].filter(Boolean).join(" · ") }),
        flags.length ? h("div", { class: "task-foot" }, flags) : null,
        h("div", { class: "review-parts" }, card.parts.length ? card.parts.map(partNode) : h("p", { class: "muted", text: "Содержимое: —" })),
      ),
      h("div", { class: "review-actions" },
        h("button", { class: "btn", type: "button", text: "✅ Принять", onClick: approveDefault }),
        h("button", { class: "btn secondary", type: "button", text: "✏️ Своя сумма", onClick: () => toggle(customBox) }),
        customBox,
        h("button", { class: "btn secondary", type: "button", text: "❌ Отклонить", onClick: () => toggle(rejectBox) }),
        rejectBox,
        h("button", { class: "btn ghost", type: "button", text: "⏭ Пропустить", onClick: () => { offset += 1; say(""); load(); } }),
      ),
    );
    setMainButton("✅ Принять", approveDefault);
  }

  await load();
}

export function unmount() {
  // MainButton снимает ядро при смене маршрута; offset пропусков живёт только на экране.
}
