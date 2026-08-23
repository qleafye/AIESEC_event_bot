// Экран сдачи (экран 3 скетча Phase 16 в вебе): делегат собирает части — фото/файлы и
// текст — в локальный черновик, видит счётчик «📸 2 · 📄 1 · ✍️ 0», может «Убрать последнее»
// (в БД на этом этапе ничего нет), MainButton «Готово» -> POST /submissions одним запросом.
//
// Файлы уходят на сервер сразу при выборе (POST /uploads -> file_id + part_token), каждый
// независимо; размер проверяется ДО отправки по лимитам из API (GET /uploads/limits) —
// текст отказа из реестра (miniapp_upload_too_large_text), чисел и текстов в JS нет.
// Пустая отправка — подсказка, черновик не сбрасывается (паритет с ботом).

const ICON = { photo: "📸", document: "📄", text: "✍️", link: "🔗" };

function counterText(parts) {
  const n = (k) => parts.filter((p) => p.kind === k).length;
  return `📸 ${n("photo")} · 📄 ${n("document")} · ✍️ ${n("text") + n("link")}`;
}

function isLink(text) {
  const t = text.trim().toLowerCase();
  return t.startsWith("http://") || t.startsWith("https://");
}

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton, tg } = ctx;
  const taskId = params.id;

  const [task, limits] = await Promise.all([
    api(`/tasks/${encodeURIComponent(taskId)}`),
    api("/uploads/limits"),
  ]);

  // Черновик: { kind, content, part_token?, caption?, status: "ready"|"uploading"|"error", label }
  const parts = [];
  let pending = 0; // файлов в полёте

  root.append(
    h("h1", { text: task.title }),
    h("p", { class: "muted", text: task.proof_hint ? `Нужно прислать: ${task.proof_hint}` : "" }),
  );

  const counter = h("div", { class: "parts-counter", text: counterText(parts) });
  const list = h("div", { class: "parts-list" });
  const notice = h("p", { class: "error-inline hidden" });

  function say(text) {
    notice.textContent = text || "";
    notice.classList.toggle("hidden", !text);
  }

  function redraw() {
    counter.textContent = counterText(parts);
    list.replaceChildren(
      ...parts.map((p, i) =>
        h("div", { class: `part-row ${p.status}` },
          h("span", { class: "part-icon", text: ICON[p.kind] || "📎" }),
          h("span", { class: "part-label", text: `${i + 1}. ${p.label}` }),
          h("span", {
            class: "part-status",
            text: p.status === "uploading" ? "загружается…" : p.status === "error" ? "ошибка" : "готово",
          }),
        ),
      ),
    );
    removeBtn.disabled = parts.length === 0;
    setMainButton("Готово", finish, { disabled: pending > 0 });
  }

  // ── файлы ──
  const fileInput = h("input", {
    type: "file",
    multiple: true,
    accept: "image/*,.pdf,.doc,.docx,.txt,.zip",
    class: "hidden",
  });

  async function uploadOne(file) {
    if (parts.length >= limits.max_parts) {
      say(`Больше ${limits.max_parts} частей в одну сдачу не влезет — нажмите «Готово».`);
      return;
    }
    if (file.size > limits.max_bytes) {
      // Проверка размера ДО отправки: текст из реестра.
      say(limits.too_large_text);
      return;
    }
    say("");
    const isPhoto = file.type.startsWith("image/") && file.size <= limits.photo_max_bytes;
    const part = { kind: isPhoto ? "photo" : "document", content: null, status: "uploading", label: file.name };
    parts.push(part);
    pending += 1;
    redraw();
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const res = await api("/uploads", { method: "POST", form });
      part.kind = res.kind;
      part.content = res.content;
      part.part_token = res.part_token;
      part.status = "ready";
    } catch (err) {
      part.status = "error";
      const idx = parts.indexOf(part);
      if (idx >= 0) parts.splice(idx, 1);
      if (err && err.status === 413) say(limits.too_large_text);
      else say(`Не удалось загрузить «${file.name}» — попробуйте ещё раз.`);
    } finally {
      pending -= 1;
      redraw();
    }
  }

  fileInput.addEventListener("change", () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = "";
    // Независимо и параллельно: одна медленная загрузка не держит остальные.
    files.forEach((f) => { uploadOne(f); });
  });

  // ── текст ──
  const textArea = h("textarea", { class: "input", rows: "3", placeholder: "Текст или ссылка…" });
  textArea.maxLength = limits.max_text;

  function addText() {
    const text = textArea.value.trim();
    if (!text) return;
    if (parts.length >= limits.max_parts) {
      say(`Больше ${limits.max_parts} частей в одну сдачу не влезет — нажмите «Готово».`);
      return;
    }
    parts.push({
      kind: isLink(text) ? "link" : "text",
      content: text.slice(0, limits.max_text),
      status: "ready",
      label: text.length > 40 ? `${text.slice(0, 40)}…` : text,
    });
    textArea.value = "";
    say("");
    redraw();
  }

  const removeBtn = h("button", {
    class: "btn secondary", type: "button", text: "Убрать последнее",
    onClick: () => {
      const last = parts[parts.length - 1];
      if (!last || last.status === "uploading") return;
      parts.pop();
      say("");
      redraw();
    },
  });

  // ── финализация ──
  let sending = false;

  async function finish() {
    if (sending) return;
    if (pending > 0) { say("Подождите, файлы ещё загружаются."); return; }
    const ready = parts.filter((p) => p.status === "ready");
    if (!ready.length) { say(limits.empty_hint); return; } // черновик не сбрасываем
    sending = true;
    setMainButton("Готово", finish, { disabled: true });
    try {
      const res = await api("/submissions", {
        method: "POST",
        body: {
          task_id: Number(taskId),
          parts: ready.map((p) => ({
            kind: p.kind, content: p.content, part_token: p.part_token || undefined,
          })),
        },
      });
      if (tg && tg.showAlert) tg.showAlert(res.accepted_text || "Сдача принята");
      navigate("#/tasks");
    } catch (err) {
      sending = false;
      if (err && err.status === 409) {
        say("Уже отправлено — обновите список заданий.");
      } else if (err && err.status === 400 && err.payload && err.payload.hint) {
        say(err.payload.hint);
      } else if (err && err.status === 400 && err.reason === "too_many_parts") {
        say(`Не больше ${err.payload.limit} частей — уберите лишние.`);
      } else if (!(err && (err.status === 401 || err.status === 403 || err.status === 503))) {
        say("Не удалось отправить — попробуйте ещё раз.");
      }
      redraw();
    }
  }

  root.append(
    h("div", { class: "card submit-card" },
      counter,
      list,
      notice,
      h("div", { class: "submit-actions" },
        h("button", { class: "btn", type: "button", text: "📎 Добавить фото или файл", onClick: () => fileInput.click() }),
        fileInput,
        h("div", { class: "field" }, textArea),
        h("button", { class: "btn secondary", type: "button", text: "✍️ Добавить текст", onClick: addText }),
        removeBtn,
      ),
    ),
    h("button", { class: "btn", type: "button", text: "✅ Готово", onClick: finish }),
  );
  redraw();
}

export function unmount() {
  // MainButton снимает ядро при смене маршрута; черновик живёт только на экране.
}
