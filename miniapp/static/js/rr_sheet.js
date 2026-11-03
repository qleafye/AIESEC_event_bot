// Полноэкранный лист подтверждений и выбора. Вызывающий передаёт h();
// этот модуль не тянет помощник сам, чтобы не склеивать сборку экрана с листом.
import { haptic, slideIn } from "./motion.js";
import { suspendBackButton } from "./app.js";

const KIND_CLASS = {
  primary: "btn",
  danger: "btn danger",
  outlined: "btn secondary",
  ghost: "btn ghost",
};

let seq = 0;
let current = null;

function focusables(sheet) {
  return [...sheet.querySelectorAll("button, a[href], input, select, textarea, [tabindex]")].filter((el) => {
    if (el.disabled) return false;
    if (el.getAttribute("tabindex") === "-1") return false;
    return true;
  });
}

export function openSheet(h, { title, body, actions, onClose, warn } = {}) {
  if (current) current.close();
  seq += 1;
  const titleId = `rr-sheet-title-${seq}`;
  const nodes = Array.isArray(body) ? body : (body ? [body] : []);
  const bodyWrap = h("div", { class: "rr-sheet__body" }, ...nodes);
  const errorEl = h("p", { class: "rr-error", hidden: true });
  const buttons = [];
  const actionNodes = [];
  for (const action of actions || []) {
    const button = h("button", {
      type: "button",
      class: KIND_CLASS[action.kind] || "btn",
      text: action.text || "",
    });
    button.addEventListener("click", () => {
      if (typeof action.onClick === "function") action.onClick();
    });
    buttons.push(button);
    actionNodes.push(button);
    if (action.note) actionNodes.push(h("p", { class: "rr-sheet__note", text: action.note }));
  }
  const sheet = h("div", {
    class: "rr-sheet",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": titleId,
  },
    h("h2", { class: "rr-sheet__title", id: titleId, text: title || "" }),
    bodyWrap,
    errorEl,
    h("div", { class: "rr-sheet__actions" }, ...actionNodes),
  );
  document.body.append(sheet);

  const safeAt = (actions || []).findIndex((action) => action.kind === "ghost" || action.kind === "outlined");
  const fallbackAt = (actions || []).findIndex((action) => action.kind !== "danger");
  const focusAt = safeAt >= 0 ? safeAt : (fallbackAt >= 0 ? fallbackAt : 0);
  if (buttons[focusAt]) buttons[focusAt].focus();

  if (warn) haptic("warning");
  slideIn(sheet, "fwd");

  let closed = false;
  let restoreBack = () => {};
  let api = null;

  function onKey(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusables(sheet);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    restoreBack();
    sheet.remove();
    if (current === api) current = null;
    if (typeof onClose === "function") onClose();
  }

  document.addEventListener("keydown", onKey);
  // Telegram зовёт все обработчики BackButton. suspendBackButton снимает историю
  // на время листа и возвращает её в close. Локальное чтение — тот же объект.
  const telegramBack = window.Telegram?.WebApp?.BackButton;
  if (telegramBack) restoreBack = suspendBackButton(close);

  function setBusy(busy) {
    sheet.setAttribute("aria-busy", busy ? "true" : "false");
    for (const button of buttons) button.disabled = Boolean(busy);
  }

  function setError(text) {
    errorEl.textContent = text || "";
    if (text) errorEl.removeAttribute("hidden");
    else errorEl.setAttribute("hidden", "");
  }

  api = { close, setBusy, setError };
  current = api;
  return api;
}
