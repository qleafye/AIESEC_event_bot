// Разметка сертификата: первая страница PDF и рамки. Шкала выбирается кнопкой.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть разметку. Проверьте интернет и нажмите «Открыть экран снова».";

function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

function emptyMarkup() {
  return {
    name: { x: 0.12, y: 0.42, w: 0.76, h: 0.08 },
    date: { x: 0.08, y: 0.8, w: 0.34, h: 0.06 },
    city: { x: 0.55, y: 0.8, w: 0.34, h: 0.06 },
    scales: [{ x: 0.55, y: 0.18, w: 0.4, h: 0.06, competency: "" }],
  };
}

function copyMarkup(raw) {
  const base = emptyMarkup();
  if (!raw) return base;
  for (const key of ["name", "date", "city"]) {
    if (raw[key]) base[key] = { ...base[key], ...raw[key] };
  }
  if (Array.isArray(raw.scales) && raw.scales.length) {
    base.scales = raw.scales.map((box) => ({
      x: box.x, y: box.y, w: box.w, h: box.h, competency: box.competency || "",
    }));
  }
  return base;
}

export async function render(root, _params, ctx) {
  const { h, api } = ctx;
  let data = null;
  let selected = 0;
  let markup = emptyMarkup();
  let aspect = 842 / 595;
  let previewUrl = "";
  let previewSeq = 0;

  const blobUrl = (url) => (url ? url.split("#")[0] : "");
  const note = (text) => {
    paint();
    root.prepend(h("p", { text }));
  };
  const TOO_LARGE = "Файл больше 20 МБ. Пришлите PDF поменьше.";

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Разметка сертификата" }),
      h("p", { text: text || FAIL }),
      h("button", {
        type: "button", class: "btn secondary", text: "Открыть экран снова",
        onClick: () => render(root, _params, ctx),
      }),
    );
  };

  const place = (el, box) => {
    el.style.left = `${box.x * 100}%`;
    el.style.top = `${box.y * 100}%`;
    el.style.width = `${box.w * 100}%`;
    el.style.height = `${box.h * 100}%`;
  };

  const watch = (el, box, mode) => {
    el.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const stage = el.closest("[data-stage]");
      const rect = stage.getBoundingClientRect();
      const startX = event.clientX;
      const startY = event.clientY;
      const orig = { ...box };
      const move = (ev) => {
        const dx = (ev.clientX - startX) / rect.width;
        const dy = (ev.clientY - startY) / rect.height;
        if (mode === "move") {
          box.x = clamp(orig.x + dx, 0, 1 - box.w);
          box.y = clamp(orig.y + dy, 0, 1 - box.h);
        } else {
          box.w = clamp(orig.w + dx, 0.04, 1 - orig.x);
          box.h = clamp(orig.h + dy, 0.03, 1 - orig.y);
        }
        place(el.parentElement === stage ? el : el.parentElement, box);
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  };

  const frame = (label, box) => {
    const el = h("div", { class: "cert-frame", text: label });
    place(el, box);
    const grip = h("span", { class: "cert-grip", text: "" });
    el.append(grip);
    watch(el, box, "move");
    watch(grip, box, "resize");
    return el;
  };

  const paint = () => {
    const templates = (data && data.templates) || [];
    const current = templates[selected];
    const nodes = [h("h1", { text: "Разметка сертификата" })];
    if (!templates.length) {
      nodes.push(h("p", { text: "Сначала загрузите PDF." }));
    }
    for (let i = 0; i < templates.length; i += 1) {
      nodes.push(h("button", {
        type: "button",
        class: i === selected ? "btn" : "btn secondary",
        text: templates[i].title,
        onClick: () => { selected = i; markup = copyMarkup(templates[i].markup); loadPreview(); },
      }));
    }
    if (current) {
      const input = h("input", { type: "file", accept: "application/pdf,.pdf" });
      input.addEventListener("change", () => sendFile(input.files && input.files[0]));
      nodes.push(h("p", { text: "Обычный PDF, без слоёв. Можно и файл из чата." }), input);
    }
    const stage = h("div", { class: "cert-stage", "data-stage": "1" });
    stage.style.aspectRatio = String(aspect);
    if (previewUrl) {
      const sheet = h("object", { class: "cert-sheet", data: previewUrl, type: "application/pdf" });
      stage.append(sheet);
    } else if (current && current.has_file) {
      stage.append(h("p", { text: "Открываю первую страницу…" }));
    } else {
      stage.append(h("p", { text: "Здесь будет первая страница PDF." }));
    }
    stage.append(frame("Имя", markup.name));
    stage.append(frame("Дата", markup.date));
    stage.append(frame("Город", markup.city));
    markup.scales.forEach((box, index) => {
      const label = box.competency ? `Шкала ${index + 1}: ${box.competency}` : `Шкала ${index + 1}`;
      stage.append(frame(label, box));
    });
    nodes.push(stage);
    const competencies = (data && data.competencies) || [];
    markup.scales.forEach((box, index) => {
      nodes.push(h("p", { text: `Шкала ${index + 1}` }));
      if (!competencies.length) {
        nodes.push(h("p", { text: "Сначала заведите компетенции в программе форума. Тогда шкалу можно выбрать кнопкой." }));
        return;
      }
      for (const name of competencies) {
        nodes.push(h("button", {
          type: "button",
          class: box.competency === name ? "btn" : "btn secondary",
          text: name,
          onClick: () => { box.competency = name; paint(); },
        }));
      }
    });
    nodes.push(h("button", {
      type: "button", class: "btn secondary", text: "Ещё шкала",
      onClick: () => {
        if (markup.scales.length >= 8) return;
        const y = Math.min(0.7, 0.18 + markup.scales.length * 0.08);
        markup.scales.push({ x: 0.55, y, w: 0.4, h: 0.06, competency: "" });
        paint();
      },
    }));
    if (markup.scales.length) {
      nodes.push(h("button", {
        type: "button", class: "btn secondary", text: "Убрать последнюю шкалу",
        onClick: () => { markup.scales.pop(); paint(); },
      }));
    }
    nodes.push(h("button", { type: "button", class: "btn", text: "Сохранить рамки", onClick: save }));
    root.replaceChildren(...nodes);
  };

  const loadPreview = async () => {
    const seq = ++previewSeq;
    const current = data.templates[selected];
    if (previewUrl) URL.revokeObjectURL(blobUrl(previewUrl));
    previewUrl = "";
    if (!current || !current.has_file) {
      paint();
      return;
    }
    paint();
    let response;
    try {
      response = await api(
        `/certificates/markup/preview?kind=${encodeURIComponent(current.kind)}&variant=${encodeURIComponent(current.variant)}`,
      );
    } catch (err) {
      if (seq !== previewSeq || isCoreHandledError(err)) return;
      note((err && err.payload && err.payload.text) || "Не получилось открыть этот PDF. Пересохраните его и пришлите ещё раз.");
      return;
    }
    if (seq !== previewSeq) return;
    if (!response || !response.ok) {
      note("Не получилось открыть этот PDF. Пересохраните его и пришлите ещё раз.");
      return;
    }
    const blob = await response.blob();
    const created = URL.createObjectURL(blob);
    if (seq !== previewSeq) {
      URL.revokeObjectURL(created);
      return;
    }
    const header = Number(response.headers.get("X-Page-Aspect"));
    if (header > 0) aspect = header;
    previewUrl = `${created}#toolbar=0&navpanes=0&view=Fit`;
    paint();
  };

  const save = async () => {
    const current = data && data.templates[selected];
    if (!current || !current.has_file) {
      fail("Сначала загрузите PDF шаблона.");
      return;
    }
    let result;
    try {
      result = await api("/certificates/markup", {
        method: "POST",
        body: { kind: current.kind, variant: current.variant, markup },
      });
    } catch (err) {
      if (isCoreHandledError(err)) return;
      note((err && err.payload && err.payload.text) || FAIL);
      return;
    }
    current.markup = copyMarkup(markup);
    note((result && result.text) || "Рамки сохранены.");
  };

  const sendFile = async (file) => {
    const current = data.templates[selected];
    if (!file || !current) return;
    const form = new FormData();
    form.append("file", file);
    let result;
    try {
      result = await api(
        `/certificates/markup/file?kind=${encodeURIComponent(current.kind)}&variant=${encodeURIComponent(current.variant)}`,
        { method: "POST", form },
      );
    } catch (err) {
      if (isCoreHandledError(err)) return;
      const text = err && err.reason === "too_large"
        ? TOO_LARGE
        : ((err && err.payload && err.payload.text) || FAIL);
      note(text);
      return;
    }
    current.has_file = true;
    if (result && result.text && result.text.includes("сняты")) markup = emptyMarkup();
    const saved = result && result.text;
    await loadPreview();
    if (saved) root.prepend(h("p", { text: saved }));
  };

  try {
    data = await api("/certificates/markup");
  } catch (err) {
    if (!isCoreHandledError(err)) fail();
    return;
  }
  const first = (data.templates || []).findIndex((item) => item.has_file);
  selected = first >= 0 ? first : 0;
  markup = copyMarkup(data.templates && data.templates[selected] && data.templates[selected].markup);
  await loadPreview();
}
