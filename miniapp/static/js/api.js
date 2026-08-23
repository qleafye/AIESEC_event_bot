// Транспорт Mini App: api() к /app/api/* и esc() для вставки текста.
//
// Заголовки: `X-Requested-With: fetch` всегда (CSRF-сторож cookie-ветки на сервере),
// `X-Telegram-Init-Data` — при непустом Telegram.WebApp.initData. Cookie дашборда (`yl_dash`)
// уходит сама: credentials "same-origin".
//
// Ошибки доступа не «чинятся» здесь — api() сообщает ядру, какой экран состояния показать,
// и бросает ApiError. На 401 ретраев НЕТ: initData не обновляется, пока приложение открыто,
// повторный запрос вернёт тот же 401, и протухшая вкладка ушла бы в цикл.

const tg = window.Telegram && window.Telegram.WebApp;

export const initData = (tg && tg.initData) || "";

export class ApiError extends Error {
  constructor(status, reason, payload) {
    super(`api ${status} ${reason}`);
    this.status = status;
    this.reason = reason;
    this.payload = payload || {};
  }
}

// Ядро регистрирует обработчик: (state, payload) => void, где state —
// "open-in-bot" | "expired" | "no-access" | "disabled".
let authErrorHandler = () => {};

export function setAuthErrorHandler(fn) {
  authErrorHandler = typeof fn === "function" ? fn : () => {};
}

export async function api(path, { method = "GET", body, form } = {}) {
  const headers = { "X-Requested-With": "fetch" };
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";

  const response = await fetch(`/app/api${path}`, {
    method,
    headers,
    body: form || (body !== undefined ? JSON.stringify(body) : undefined),
    credentials: "same-origin",
  });

  if (response.ok) {
    if (response.status === 204) return null;
    const type = response.headers.get("Content-Type") || "";
    return type.includes("application/json") ? response.json() : response;
  }

  const payload = await response.json().catch(() => ({}));
  const reason = payload.reason || "error";

  if (response.status === 401) {
    // bad_initdata — подпись/срок: «Сессия истекла»; no_auth — нет ни initData, ни cookie:
    // «Откройте через бота». Без повторной попытки (см. шапку файла).
    authErrorHandler(reason === "bad_initdata" ? "expired" : "open-in-bot", payload);
  } else if (response.status === 403) {
    // staff_only / no_cap / section_off / delegate_gate / csrf — экран «Нет доступа».
    authErrorHandler("no-access", payload);
  } else if (response.status === 503 && reason === "miniapp_off") {
    authErrorHandler("disabled", payload);
  }
  throw new ApiError(response.status, reason, payload);
}

// Экранирование для текста из БД/реестра. Правило: в DOM текст попадает только через
// textContent или esc(); innerHTML с интерполяцией запрещён (сторожевой тест).
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ESC[ch]);
}
