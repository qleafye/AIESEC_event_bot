// Карточка задания менеджера — один экран на два режима (экраны 6 и 7 скетча Phase 16,
// editorial-минимал 19.1-06).
//
// «Правка» (#/task-edit/{id}): превью карточки делегата — та же вёрстка, что у card.js (план
// 19.1-05): обложка во всю ширину, заголовок, Label-роль меты, чипы типа подтверждения,
// описание. Точечные правки — плоский список (D-11, ui.js::flatRow): «подпись — значение —
// карандаш», тап открывает панель с одним полем и кнопкой сохранения (один PATCH на поле).
// Обложка (загрузка через /uploads, затем PATCH с photo_file_id + part_token), «В архив» /
// «Вернуть», «Удалить» — отдельные действия ниже списка. Каждая правка — одно поле, один
// PATCH. Архив и удаление — двухшаговое подтверждение с текстом, что именно произойдёт;
// удаление недоступно при can_delete=false с объяснением причины (а не молча).
//
// «Создание» (#/task-edit/new): те же поля пошагово; категория, типы подтверждения, город и
// пресеты дедлайна — кнопки/чекбоксы сеткой с подписями из GET /admin/tasks/options (фронт
// кодов не знает и человеку их не показывает); последний шаг — превью и «Опубликовать».
//
// Ошибки сервера показываются человеческим текстом из payload.text, не кодом ответа.

import { flatRow } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";

// Иконка по типу подтверждения — та же карта, что у card.js (план 19.1-05): технический
// код -> имя иконки, не человеческая подпись (подписи по-прежнему только с сервера).
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

function fileUrl(fileId) {
  return `/app/api/file/${encodeURIComponent(fileId)}`;
}

function errorText(err, fallback) {
  if (err && err.payload && err.payload.text) return err.payload.text;
  return fallback;
}

function isAuthError(err) {
  return Boolean(err && (err.status === 401 || err.status === 403 && err.reason !== "bad_part_token" && err.reason !== "out_of_scope" || err.status === 503));
}

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton, tg } = ctx;
  const isNew = params.id === "new";

  const notice = h("p", { class: "chip accent hidden" });
  const holder = h("div");
  root.append(notice, holder);

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "accent"}${text ? "" : " hidden"}`;
  }

  // ── превью (та же вёрстка, что у карточки делегата, план 19.1-05) ──
  function preview(card, photoFileId) {
    const box = h("article", { class: "card task-preview" });
    if (photoFileId) {
      const img = h("img", { class: "cover", alt: "", src: fileUrl(photoFileId) });
      img.addEventListener("error", () => img.remove());
      box.append(img);
    }
    box.append(h("p", { class: "faint" }, icon("user"), h("span", { text: " Так увидит делегат" })));
    if (card.title) box.append(h("h1", { text: card.title }));
    const metaParts = [card.category_label, card.coins != null ? `${card.coins} монет` : null, card.deadline_display ? `до ${card.deadline_display}` : null].filter(Boolean);
    if (metaParts.length) box.append(h("p", { class: "label-role", text: metaParts.join(" · ") }));
    const chips = proofChips(h, card.proof_type);
    if (chips) box.append(chips);
    if (card.city_label) box.append(h("p", { class: "muted", text: `Кому: ${card.city_label}` }));
    if (card.text) box.append(h("div", { class: "pre", text: card.text }));
    return box;
  }

  // ── загрузка обложки: размер проверяется ДО отправки по лимитам из API ──
  async function uploadCover(file, limits) {
    if (!file.type.startsWith("image/")) {
      say("Обложка должна быть картинкой — файл другого типа не подойдёт.", "warn");
      return null;
    }
    if (file.size > limits.photo_max_bytes) {
      say(limits.too_large_text, "warn");
      return null;
    }
    const form = new FormData();
    form.append("file", file, file.name);
    const res = await api("/uploads", { method: "POST", form });
    if (res.kind !== "photo") {
      say("Обложка должна быть картинкой — файл другого типа не подойдёт.", "warn");
      return null;
    }
    return { photo_file_id: res.content, part_token: res.part_token };
  }

  function deadlinePicker(presets, example, onPick) {
    const custom = h("input", { class: "input", type: "text", placeholder: example, inputmode: "numeric" });
    return h("div", { class: "choice-grid" },
      presets.map((p) => h("button", { class: "btn secondary", type: "button", text: p.label, onClick: () => onPick(p.code) })),
      h("div", { class: "field" },
        h("label", { text: `Своя дата — ДД.ММ.ГГГГ ЧЧ:ММ, например ${example}` }),
        custom,
        h("button", { class: "btn", type: "button", text: "Поставить", onClick: () => onPick(custom.value.trim()) }),
      ),
    );
  }

  // ════════════════════════════════════════════════════════════════════════════════════
  // Режим «правка»
  // ════════════════════════════════════════════════════════════════════════════════════
  async function renderEdit(taskId) {
    let card;
    try {
      card = await api(`/admin/tasks/${encodeURIComponent(taskId)}`);
    } catch (err) {
      if (err && err.status === 404) {
        holder.replaceChildren(h("div", { class: "empty" }, h("p", { text: "Задание не найдено — возможно, его уже удалили." }),
          h("button", { class: "btn ghost", type: "button", text: "← К заданиям", onClick: () => navigate("#/admin-tasks") })));
      } else if (!isAuthError(err)) {
        holder.replaceChildren(h("p", { class: "error-inline", text: errorText(err, "Не удалось открыть задание — попробуйте ещё раз.") }));
      }
      return;
    }
    let busy = false;
    let options = null;
    let limits = null;

    async function patch(body, okText) {
      if (busy) return false;
      busy = true;
      try {
        const res = await api(`/admin/tasks/${encodeURIComponent(taskId)}`, { method: "PATCH", body });
        card = res.task;
        say(okText, "success");
        haptic("success");
        draw();
        return true;
      } catch (err) {
        if (!isAuthError(err)) say(errorText(err, "Не получилось сохранить — попробуйте ещё раз."), "warn");
        return false;
      } finally {
        busy = false;
      }
    }

    function panel(label, control, saveText, onSave) {
      const box = h("div", { class: "field hidden" },
        h("label", { text: label }),
        control,
        h("button", { class: "btn", type: "button", text: saveText, onClick: onSave }),
      );
      return box;
    }

    function draw() {
      const panels = [];
      function toggle(box) {
        const wasHidden = box.classList.contains("hidden");
        panels.forEach((p) => p.classList.add("hidden"));
        if (wasHidden) box.classList.remove("hidden");
      }

      // Название
      const titleInput = h("input", { class: "input", type: "text", maxlength: "60" });
      titleInput.value = card.title;
      const titleBox = panel("Название (до 60 символов)", titleInput, "Сохранить", () => patch({ title: titleInput.value }, "Название обновлено."));

      // Описание
      const textArea = h("textarea", { class: "input", rows: "5" });
      textArea.value = card.text;
      const textBox = panel("Описание — что нужно сделать", textArea, "Сохранить", () => patch({ text: textArea.value }, "Описание обновлено."));

      // Монеты
      const coinsInput = h("input", { class: "input", type: "number", min: "1", step: "1", inputmode: "numeric" });
      coinsInput.value = String(card.coins);
      const coinsBox = panel("Сколько монет за задание? Например 10", coinsInput, "Сохранить", () => patch({ coins: Number(coinsInput.value) }, `Монеты обновлены: ${coinsInput.value}.`));

      // Дедлайн — пресеты + своя дата
      const deadlineBox = h("div", { class: "field hidden" }, h("label", { text: `Сейчас: до ${card.deadline_display}` }));
      async function openDeadline() {
        toggle(deadlineBox);
        if (deadlineBox.classList.contains("hidden")) return;
        if (!options) options = await api("/admin/tasks/options");
        deadlineBox.replaceChildren(
          h("label", { text: `Сейчас: до ${card.deadline_display}` }),
          deadlinePicker(options.deadline_presets, options.deadline_example, (value) => patch({ deadline_at: value }, "Дедлайн обновлён.")),
        );
      }

      // Обложка
      const fileInput = h("input", { type: "file", accept: "image/*", class: "hidden" });
      fileInput.addEventListener("change", async () => {
        const file = (fileInput.files || [])[0];
        fileInput.value = "";
        if (!file) return;
        try {
          if (!limits) limits = await api("/uploads/limits");
          say("Загружаем фото…", "accent");
          const up = await uploadCover(file, limits);
          if (!up) return;
          await patch({ photo_file_id: up.photo_file_id, part_token: up.part_token }, "Обложка обновлена.");
        } catch (err) {
          if (err && err.status === 413 && limits) say(limits.too_large_text, "warn");
          else if (!isAuthError(err)) say(errorText(err, "Не удалось загрузить фото — попробуйте ещё раз."), "warn");
        }
      });

      // Плоский список точечных правок (D-11): подпись — значение — карандаш.
      const pointsList = h("div", { class: "flat-list" },
        flatRow(h, { title: "Название", meta: card.title, trailing: icon("pen-line"), onClick: () => toggle(titleBox) }),
        flatRow(h, { title: "Описание", meta: card.text, trailing: icon("pen-line"), onClick: () => toggle(textBox) }),
        flatRow(h, { title: "Монеты", meta: `${card.coins} монет`, trailing: icon("pen-line"), onClick: () => toggle(coinsBox) }),
        flatRow(h, { title: "Дедлайн", meta: `до ${card.deadline_display}`, trailing: icon("pen-line"), onClick: openDeadline }),
        flatRow(h, { title: "Обложка", meta: card.photo_file_id ? "Загружена" : "Не добавлена", trailing: icon("image"), onClick: () => fileInput.click() }),
      );

      // В архив — двухшаговое подтверждение с описанием последствий
      const archiveConfirm = h("div", { class: "confirm-box hidden" },
        h("p", { text: "Задание уйдёт в архив: делегаты перестанут его видеть и сдавать. Уже присланные сдачи останутся на проверке. Вернуть можно в любой момент." }),
        h("button", { class: "btn danger", type: "button", text: "Да, в архив", onClick: async () => {
          if (busy) return;
          busy = true;
          try {
            await api(`/admin/tasks/${encodeURIComponent(taskId)}/archive`, { method: "POST" });
            card = await api(`/admin/tasks/${encodeURIComponent(taskId)}`);
            say("Задание в архиве.", "success");
            haptic("success");
            draw();
          } catch (err) {
            if (!isAuthError(err)) say(errorText(err, "Не получилось — попробуйте ещё раз."), "warn");
          } finally { busy = false; }
        } }),
        h("button", { class: "btn ghost", type: "button", text: "Отмена", onClick: () => archiveConfirm.classList.add("hidden") }),
      );

      // Вернуть — безопасная операция, без подтверждения (как в боте)
      async function unarchive() {
        if (busy) return;
        busy = true;
        try {
          await api(`/admin/tasks/${encodeURIComponent(taskId)}/unarchive`, { method: "POST" });
          card = await api(`/admin/tasks/${encodeURIComponent(taskId)}`);
          say("Задание снова активно.", "success");
          haptic("success");
          draw();
        } catch (err) {
          if (!isAuthError(err)) say(errorText(err, "Не получилось — попробуйте ещё раз."), "warn");
        } finally { busy = false; }
      }

      // Удалить — двухшаговое подтверждение; недоступно при сдачах с объяснением
      const deleteConfirm = h("div", { class: "confirm-box hidden" },
        h("p", { text: "Задание будет удалено навсегда — восстановить его не получится. Сдач у него нет, поэтому ничего больше не пропадёт." }),
        h("button", { class: "btn danger", type: "button", text: "Да, удалить", onClick: async () => {
          if (busy) return;
          busy = true;
          try {
            await api(`/admin/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
            haptic("success");
            navigate("#/admin-tasks");
          } catch (err) {
            if (err && err.status === 409) {
              say(errorText(err, "У задания уже есть сдачи — его можно только убрать в архив."), "warn");
              card = await api(`/admin/tasks/${encodeURIComponent(taskId)}`);
              draw();
            } else if (!isAuthError(err)) say(errorText(err, "Не получилось — попробуйте ещё раз."), "warn");
          } finally { busy = false; }
        } }),
        h("button", { class: "btn ghost", type: "button", text: "Отмена", onClick: () => deleteConfirm.classList.add("hidden") }),
      );

      panels.push(titleBox, textBox, coinsBox, deadlineBox, archiveConfirm, deleteConfirm);

      const deleteBtn = h("button", {
        class: "btn secondary", type: "button",
        disabled: !card.can_delete,
        onClick: () => toggle(deleteConfirm),
      }, icon("trash-2"), h("span", { text: " Удалить" }));

      holder.replaceChildren(...[
        h("h1", { text: card.title }),
        card.archived ? h("p", { class: "chip" }, icon("archive"), h("span", { text: " В архиве — делегаты его не видят" })) : null,
        preview(card, card.photo_file_id),
        h("p", { class: "muted", text: `Сдач: ${card.submissions_count}` }),
        pointsList,
        titleBox, textBox, coinsBox, deadlineBox, fileInput,
        h("div", { class: "task-actions" },
          card.archived
            ? h("button", { class: "btn secondary", type: "button", onClick: unarchive }, icon("rotate-ccw"), h("span", { text: " Вернуть из архива" }))
            : h("button", { class: "btn secondary", type: "button", onClick: () => toggle(archiveConfirm) }, icon("archive"), h("span", { text: " В архив" })),
          archiveConfirm,
          deleteBtn,
          card.can_delete ? null : h("p", { class: "faint", text: card.cannot_delete_text || "У задания есть сдачи — удалить нельзя, только в архив." }),
          deleteConfirm,
          h("button", { class: "btn ghost", type: "button", text: "← К заданиям", onClick: () => navigate("#/admin-tasks") }),
        ),
      ].filter(Boolean));
      setMainButton(null);
    }

    draw();
  }

  // ════════════════════════════════════════════════════════════════════════════════════
  // Режим «создание»: шаги по одному, выбор — сеткой кнопок, текст — только название/описание/сумма
  // ════════════════════════════════════════════════════════════════════════════════════
  async function renderCreate() {
    let options;
    try {
      options = await api("/admin/tasks/options");
    } catch (err) {
      if (!isAuthError(err)) holder.replaceChildren(h("p", { class: "error-inline", text: "Не удалось открыть форму — попробуйте ещё раз." }));
      return;
    }
    const draft = {
      title: "", text: "", category: null, proof_types: [], coins: null,
      event_city: options.city_choice ? "all" : null, deadline_at: null,
      photo_file_id: null, part_token: null,
    };
    const labels = { category: "", proof: "", city: options.bound_city_label || "", deadline: "" };
    const steps = ["title", "text", "category", "proof", "coins"];
    if (options.city_choice) steps.push("city");
    steps.push("deadline", "photo", "preview");
    let stepIndex = 0;
    let busy = false;
    let limits = null;

    function step(label, ...children) {
      return h("div", { class: "wizard-step" },
        h("p", { class: "label-role", text: `Шаг ${stepIndex + 1} из ${steps.length} · ${label}` }),
        children,
      );
    }
    function navRow(onNext, nextText) {
      return h("div", { class: "task-actions" },
        h("button", { class: "btn", type: "button", text: nextText || "Далее →", onClick: onNext }),
        stepIndex > 0
          ? h("button", { class: "btn ghost", type: "button", text: "Назад", onClick: () => { stepIndex -= 1; say(""); draw(); } })
          : h("button", { class: "btn ghost", type: "button", text: "Отмена", onClick: () => navigate("#/admin-tasks") }),
      );
    }
    function next() { stepIndex += 1; say(""); draw(); }

    function draw() {
      setMainButton(null);
      const name = steps[stepIndex];
      let body;

      if (name === "title") {
        const input = h("input", { class: "input", type: "text", maxlength: String(options.title_max) });
        input.value = draft.title;
        body = step("Название", h("div", { class: "field" }, h("label", { text: `Короткое название, до ${options.title_max} символов — например «Фото со стендом»` }), input),
          navRow(() => {
            const v = input.value.trim();
            if (!v) { say("Название не может быть пустым.", "warn"); return; }
            draft.title = v; next();
          }));
      } else if (name === "text") {
        const area = h("textarea", { class: "input", rows: "5", maxlength: String(options.text_max) });
        area.value = draft.text;
        body = step("Описание", h("div", { class: "field" }, h("label", { text: "Что нужно сделать и что прислать" }), area),
          navRow(() => {
            const v = area.value.trim();
            if (!v) { say("Описание не может быть пустым.", "warn"); return; }
            draft.text = v; next();
          }));
      } else if (name === "category") {
        body = step("Категория",
          h("div", { class: "choice-grid" },
            options.categories.map((c) => h("button", {
              class: `btn secondary${draft.category === c.code ? " chosen" : ""}`, type: "button", text: c.label,
              onClick: () => { draft.category = c.code; labels.category = c.label; next(); },
            })),
          ),
          navRow(() => { if (!draft.category) { say("Выберите категорию.", "warn"); return; } next(); }));
      } else if (name === "proof") {
        const pairs = options.proof_types.map((p) => {
          const box = h("input", { type: "checkbox" });
          box.checked = draft.proof_types.includes(p.code);
          return { code: p.code, label: p.label, box, row: h("label", { class: "check" }, box, h("span", { text: p.label })) };
        });
        body = step("Что прислать в подтверждение",
          h("p", { class: "muted", text: "Можно несколько или ни одного — тогда подойдёт любое." }),
          h("div", { class: "choice-grid" }, pairs.map((p) => p.row)),
          navRow(() => {
            const chosen = pairs.filter((p) => p.box.checked);
            draft.proof_types = chosen.map((p) => p.code);
            labels.proof = chosen.map((p) => p.label).join(" + ");
            next();
          }));
      } else if (name === "coins") {
        const input = h("input", { class: "input", type: "number", min: "1", step: "1", inputmode: "numeric" });
        if (draft.coins != null) input.value = String(draft.coins);
        body = step("Монеты", h("div", { class: "field" }, h("label", { text: "Сколько монет за задание? Например 10" }), input),
          navRow(() => {
            const v = Number(input.value);
            if (!Number.isInteger(v) || v <= 0) { say("Монеты — целое число больше нуля, например 10.", "warn"); return; }
            draft.coins = v; next();
          }));
      } else if (name === "city") {
        const choices = [{ code: "all", label: "Все города" }, ...options.cities];
        body = step("Кому задание?",
          h("div", { class: "choice-grid" },
            choices.map((c) => h("button", {
              class: `btn secondary${draft.event_city === c.code ? " chosen" : ""}`, type: "button", text: c.label,
              onClick: () => { draft.event_city = c.code; labels.city = c.code === "all" ? "" : c.label; next(); },
            })),
          ),
          navRow(next));
      } else if (name === "deadline") {
        body = step("Дедлайн сдачи",
          draft.deadline_at ? h("p", { class: "muted", text: `Выбрано: ${labels.deadline}` }) : null,
          deadlinePicker(options.deadline_presets, options.deadline_example, (value) => {
            if (!value) { say("Выберите вариант или введите дату.", "warn"); return; }
            const preset = options.deadline_presets.find((p) => p.code === value);
            draft.deadline_at = value;
            labels.deadline = preset ? preset.label : value;
            next();
          }),
          navRow(() => { if (!draft.deadline_at) { say("Выберите дедлайн.", "warn"); return; } next(); }));
      } else if (name === "photo") {
        const fileInput = h("input", { type: "file", accept: "image/*", class: "hidden" });
        fileInput.addEventListener("change", async () => {
          const file = (fileInput.files || [])[0];
          fileInput.value = "";
          if (!file || busy) return;
          busy = true;
          try {
            if (!limits) limits = await api("/uploads/limits");
            say("Загружаем фото…", "accent");
            const up = await uploadCover(file, limits);
            if (!up) return;
            draft.photo_file_id = up.photo_file_id;
            draft.part_token = up.part_token;
            say("Обложка загружена.", "success");
            draw();
          } catch (err) {
            if (err && err.status === 413 && limits) say(limits.too_large_text, "warn");
            else if (!isAuthError(err)) say(errorText(err, "Не удалось загрузить фото — попробуйте ещё раз."), "warn");
          } finally { busy = false; }
        });
        body = step("Обложка (необязательно)",
          draft.photo_file_id ? preview({}, draft.photo_file_id) : null,
          h("div", { class: "task-actions" },
            h("button", { class: "btn secondary", type: "button", onClick: () => fileInput.click() }, icon("image"), h("span", { text: draft.photo_file_id ? " Заменить фото" : " Добавить фото" })),
            fileInput,
            draft.photo_file_id ? h("button", { class: "btn ghost", type: "button", onClick: () => { draft.photo_file_id = null; draft.part_token = null; draw(); } }, icon("x"), h("span", { text: " Убрать фото" })) : null,
          ),
          navRow(next, draft.photo_file_id ? "Далее →" : "Без фото"));
      } else {
        // Превью перед публикацией — из черновика, теми же подписями, что выбрал менеджер;
        // та же вёрстка, что у карточки делегата (preview()).
        const previewData = {
          title: draft.title,
          category_label: labels.category,
          coins: draft.coins,
          deadline_display: labels.deadline,
          proof_type: draft.proof_types.join(","),
          text: draft.text,
          city_label: labels.city || null,
        };
        const publish = async () => {
          if (busy) return;
          busy = true;
          setMainButton("Опубликовать", publish, { disabled: true });
          try {
            const res = await api("/admin/tasks", { method: "POST", body: {
              title: draft.title, text: draft.text, category: draft.category, coins: draft.coins,
              proof_types: draft.proof_types, deadline_at: draft.deadline_at,
              event_city: draft.event_city, photo_file_id: draft.photo_file_id || undefined,
              part_token: draft.part_token || undefined,
            } });
            haptic("success");
            if (tg && tg.showAlert) tg.showAlert("Задание создано");
            navigate(`#/task-edit/${res.id}`);
          } catch (err) {
            busy = false;
            setMainButton("Опубликовать", publish);
            if (!isAuthError(err)) say(errorText(err, "Не удалось опубликовать — проверьте поля и попробуйте ещё раз."), "warn");
          }
        };
        body = step("Проверьте и опубликуйте",
          preview(previewData, draft.photo_file_id),
          h("div", { class: "task-actions" },
            h("button", { class: "btn", type: "button", onClick: publish }, icon("check"), h("span", { text: " Опубликовать" })),
            h("button", { class: "btn ghost", type: "button", text: "Назад", onClick: () => { stepIndex -= 1; say(""); draw(); } }),
            h("button", { class: "btn ghost", type: "button", text: "Отмена", onClick: () => navigate("#/admin-tasks") }),
          ));
        setMainButton("Опубликовать", publish);
      }

      holder.replaceChildren(h("h1", { text: "Новое задание" }), body);
    }

    draw();
  }

  if (isNew) await renderCreate();
  else await renderEdit(params.id);
}

export function unmount() {
  // MainButton снимает ядро при смене маршрута; черновик создания живёт только на экране.
}
