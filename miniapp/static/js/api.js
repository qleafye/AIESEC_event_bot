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

// Запрос оборван по таймауту: сервер не ответил за timeoutMs. HTTP-статуса нет — экраны,
// различающие «сеть не ответила» по отсутствию числового `.status`, видят его как сетевую ошибку.
export class ApiTimeout extends Error {
  constructor(ms) {
    super(`api timeout ${ms}ms`);
    this.timeout = true;
  }
}

// timeoutMs — для запросов, которые нельзя ждать бесконечно (скан у двери): на «подвисшей»
// сети fetch без сигнала висит минутами. Без timeoutMs поведение прежнее.
// quiet — фоновый запрос (подсказка/плитка на главной): его 403 не уводит весь экран в
// «Нет доступа», а только бросает ApiError — вызывающий молча рисует экран без этой строки.
export async function api(path, { method = "GET", body, form, timeoutMs, quiet = false } = {}) {
  const headers = { "X-Requested-With": "fetch" };
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";

  const controller = timeoutMs && typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  let response;
  try {
    response = await fetch(`/app/api${path}`, {
      method,
      headers,
      body: form || (body !== undefined ? JSON.stringify(body) : undefined),
      credentials: "same-origin",
      signal: controller ? controller.signal : undefined,
    });
  } catch (err) {
    if (controller && controller.signal.aborted) throw new ApiTimeout(timeoutMs);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }

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
  } else if (response.status === 403 && !quiet) {
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
