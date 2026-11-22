// Рамки на картинке макета QR. Код нельзя убрать. Имя, город и ВК — по желанию.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть рамки. Проверьте интернет и нажмите «Открыть экран снова».";
const NO_FILE = "Сначала загрузите картинку макета в боте.";

function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

const OPTIONAL = {
  name: { label: "Имя", box: { x: 0.08, y: 0.72, w: 0.84, h: 0.08 } },
  city: { label: "Город", box: { x: 0.08, y: 0.82, w: 0.4, h: 0.08 } },
  vk: { label: "ВК", box: { x: 0.52, y: 0.82, w: 0.4, h: 0.08 } },
};

function defaultQr() {
  return { x: 0.2, y: 0.15, w: 0.6, h: 0.6 };
}

function copyBox(raw, fallback) {
  const box = { ...fallback };
  if (raw) Object.assign(box, raw);
  return box;
}

export async function render(root, _params, ctx) {
  const { h, api } = ctx;
  let data = null;
  let markup = { qr: defaultQr() };
  let aspect = 1;
  let previewUrl = "";

  const note = (text) => {
    paint();
    root.prepend(h("p", { text }));
  };

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Рамки на макете QR" }),
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
    const nodes = [h("h1", { text: "Рамки на макете QR" })];
    if (!data || !data.has_file) {
      nodes.push(h("p", { text: NO_FILE }));
      root.replaceChildren(...nodes);
      return;
    }
    nodes.push(h("p", { text: "Код бот ставит сам. Рамку кода убрать нельзя." }));
    const stage = h("div", { class: "cert-stage", "data-stage": "1" });
    stage.style.aspectRatio = String(aspect);
    if (previewUrl) {
      stage.append(h("img", { class: "cert-sheet", src: previewUrl, alt: "Макет карточки" }));
    } else {
      stage.append(h("p", { text: "Открываю картинку…" }));
    }
    stage.append(frame("Код", markup.qr));
    for (const key of Object.keys(OPTIONAL)) {
      if (markup[key]) stage.append(frame(OPTIONAL[key].label, markup[key]));
    }
    nodes.push(stage);
    for (const key of Object.keys(OPTIONAL)) {
      const label = OPTIONAL[key].label;
      if (markup[key]) {
        nodes.push(h("button", {
          type: "button", class: "btn secondary", text: `Убрать «${label}»`,
          onClick: () => { delete markup[key]; paint(); },
        }));
      } else {
        nodes.push(h("button", {
          type: "button", class: "btn secondary", text: `Добавить «${label}»`,
          onClick: () => { markup[key] = { ...OPTIONAL[key].box }; paint(); },
        }));
      }
    }
    nodes.push(h("button", { type: "button", class: "btn", text: "Сохранить рамки", onClick: save }));
    root.replaceChildren(...nodes);
  };

  const loadPreview = async () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = "";
    if (!data || !data.has_file) {
      paint();
      return;
    }
    paint();
    let response;
    try {
      response = await api("/qr-card-layout/preview");
    } catch (err) {
      if (isCoreHandledError(err)) return;
      note((err && err.payload && err.payload.text) || FAIL);
      return;
    }
    if (!response || !response.ok) {
      note(FAIL);
      return;
    }
    const blob = await response.blob();
    previewUrl = URL.createObjectURL(blob);
    const header = Number(response.headers.get("X-Page-Aspect"));
    if (header > 0) aspect = header;
    paint();
  };

  const save = async () => {
    if (!data || !data.has_file) {
      fail(NO_FILE);
      return;
    }
    const body = { qr: markup.qr };
    for (const key of Object.keys(OPTIONAL)) {
      if (markup[key]) body[key] = markup[key];
    }
    let result;
    try {
      result = await api("/qr-card-layout", { method: "POST", body: { markup: body } });
    } catch (err) {
      if (isCoreHandledError(err)) return;
      note((err && err.payload && err.payload.text) || FAIL);
      return;
    }
    note((result && result.text) || "Рамки сохранены.");
  };

  try {
    data = await api("/qr-card-layout");
  } catch (err) {
    if (isCoreHandledError(err)) return;
    fail((err && err.payload && err.payload.text) || FAIL);
    return;
  }
  const raw = (data && data.markup) || {};
  markup = { qr: copyBox(raw.qr, defaultQr()) };
  for (const key of Object.keys(OPTIONAL)) {
    if (raw[key]) markup[key] = copyBox(raw[key], OPTIONAL[key].box);
  }
  await loadPreview();
}
