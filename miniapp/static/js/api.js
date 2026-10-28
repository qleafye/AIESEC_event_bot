// Транспорт Mini App: api() к /app/api/* и esc() для вставки текста.
//
// Заголовки: `X-Requested-With: fetch` всегда (CSRF-сторож cookie-ветки на сервере),
// `X-Telegram-Init-Data` — при непустом Telegram.WebApp.initData. Cookie дашборда (`yl_dash`)
// уходит сама: credentials "same-origin".
//
// Ошибки доступа не «чинятся» здесь — api() сообщает ядру, какой экран состояния показать,
// и бросает ApiError. На 401 ретраев НЕТ: initData не обновляется, пока приложение открыто,
// повторный запрос вернёт тот же 401, и протухшая вкладка ушла бы в цикл.
//
// Замер загрузки файла (backlog п.25): сервер видит только свою часть (приём тела и отправку в
// Telegram), а путь телефон → Cloudflare → туннель — нет. Поэтому POST /uploads меряется здесь,
// вокруг fetch (performance.now() до запроса и после прихода заголовков или отказа), и время
// уходит отдельным POST /app/api/uploads/timing вместе с `X-Upload-Ref` ответа — сервер ставит
// его в лог рядом со своими этапами. Отчёт fire-and-forget и НЕ через api(): его 401/403/503 не
// должны уводить экран в «Нет доступа», а загрузка не ждёт отчёт. Экраны вызывают api() как
// раньше — замер живёт только здесь.

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
const UPLOAD_REF_RE = /^[0-9a-f]{12}$/;

function nowMs() {
  return typeof performance !== "undefined" && performance && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

function isTimedUpload(path, method) {
  return method === "POST" && (path === "/uploads" || path.startsWith("/uploads?"));
}

function uploadTarget(path) {
  const query = path.split("?")[1] || "";
  return new URLSearchParams(query).get("target") || "task";
}

function uploadSize(form) {
  try {
    const file = form && typeof form.get === "function" ? form.get("file") : null;
    return file && typeof file.size === "number" ? file.size : null;
  } catch (_) {
    return null;
  }
}

function reportUploadTiming(path, form, started, response) {
  const status = response ? response.status : 0;
  if (status === 401) return; // initData протухла — отчёт получил бы тот же 401
  let ref = null;
  try {
    const raw = response && response.headers ? response.headers.get("X-Upload-Ref") : null;
    ref = typeof raw === "string" && UPLOAD_REF_RE.test(raw) ? raw : null;
  } catch (_) {
    ref = null;
  }
  const payload = {
    ref,
    client_ms: Math.max(0, Math.round(nowMs() - started)),
    status,
    target: uploadTarget(path),
    size: uploadSize(form),
  };
  const headers = { "X-Requested-With": "fetch", "Content-Type": "application/json" };
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  try {
    const sent = fetch("/app/api/uploads/timing", {
      method: "POST",
      keepalive: true,
      headers,
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });
    if (sent && typeof sent.catch === "function") sent.catch(() => {});
  } catch (_) {
    // отчёт — только диагностика: его сбой не касается загрузки
  }
}

export async function api(path, { method = "GET", body, form, timeoutMs, quiet = false } = {}) {
  const headers = { "X-Requested-With": "fetch" };
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";

  const timed = isTimedUpload(path, method);
  const started = timed ? nowMs() : 0;
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
    if (timed) reportUploadTiming(path, form, started, null);
    if (controller && controller.signal.aborted) throw new ApiTimeout(timeoutMs);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (timed) reportUploadTiming(path, form, started, response);

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
