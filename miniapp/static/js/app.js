// Ядро Mini App: bootstrap Telegram WebApp SDK, hash-роутер по таблице ROUTES, навигация
// по правам и экраны состояний. Экраны приложения живут в screens/*.js и подгружаются
// лениво; каждый модуль экспортирует render(root, params, ctx) и НИЧЕГО не правит здесь.
//
// Контракт экранного модуля:
//   export async function render(root, params, ctx)
//     root   — элемент #screen (уже очищен);
//     params — параметры маршрута ({ id } и т.п.);
//     ctx    — { me, tg, h, esc, api, setMainButton, navigate, showState }.
//   Необязательно: export function unmount() — при уходе с экрана.
//
// Весь текст из БД/реестра в DOM идёт через textContent (помощник h()) — innerHTML с
// интерполяцией запрещён (сторожевой тест, T-19-07).

import { api, ApiError, esc, setAuthErrorHandler } from "./api.js";
import { icon } from "./icons.js";
import { applyMotionTier } from "./motion.js";

const tg = window.Telegram && window.Telegram.WebApp;
const root = document.documentElement;
const body = document.body;
const ds = body.dataset;
const screenEl = document.getElementById("screen");
const navEl = document.getElementById("nav");

// Раскладка навигации менеджера (D-10, голосование команды не закрыто): "tabbar" (A, нижний
// таб-бар) / "toptabs" (B, верхние табы) / "hub" (C, счётчики на плитках). Правится ОДНА эта
// строка — все три раскладки уже свёрстаны в app.css (body[data-nav="…"]), смена варианта
// после голосования не требует правки ни одного экрана.
const NAV_LAYOUT = "hub";
body.dataset.nav = NAV_LAYOUT;

// ── таблица маршрутов (один в один с планом фазы; {id} — параметр) ──────────────────────
export const ROUTES = [
  ["#/hub", "screens/hub.js"],
  ["#/tasks", "screens/tasks.js"],
  ["#/task/{id}", "screens/card.js"],
  ["#/profile", "screens/profile.js"],
  ["#/coins", "screens/coins.js"],
  ["#/leaderboard", "screens/leaderboard.js"],
  ["#/submit/{id}", "screens/submit.js"],
  ["#/review", "screens/review.js"],
  ["#/stats", "screens/stats.js"],
  ["#/admin-tasks", "screens/admin_tasks.js"],
  // Найдено планом 19.1-08 (визуальная сверка): отдельная литеральная запись «#/task-edit/new»
  // стояла ПЕРЕД «#/task-edit/{id}» и перехватывала маршрут первой — params.id оставался
  // undefined (у литерального паттерна нет {id}), а task_edit.js::isNew сравнивает именно со
  // строкой "new" -> сравнение всегда ложно, мастер создания реально уходил в renderEdit(undefined)
  // и падал на /admin/tasks/undefined (422). "new" и так проходит [^/]+ у {id} — вторая запись
  // не нужна, params.id === "new" срабатывает сама.
  ["#/task-edit/{id}", "screens/task_edit.js"],
  ["#/admin-coins", "screens/admin_coins.js"],
  ["#/settings", "screens/settings.js"],
];

// Разделы админки (Phase 20, ADMIN-IA-04, D-05): те же восемь разделов и ровно те же подписи,
// что менеджер видит в корне `/admin`. ИСТОЧНИК ПРАВДЫ — `handlers/admin_sections.py::SECTIONS`;
// расхождение подписи или порядка роняет тест `test_section_groups_match_bot_sections`, а не
// всплывает у менеджера двумя разными картами админки.
// Список пар, а не объект: порядок разделов на экране — контракт (путь делегата), а порядок
// ключей объекта в JS контрактом не является.
// Подписи ВКЛАДОК этим списком НЕ подменяются — они по-прежнему приходят из реестра
// (`miniapp_section_*`) через `body.dataset.sectionLabels`, см. hub.js::sectionLabelsFromDom.
export const SECTION_GROUPS = [
  ["event", "🎪 Событие"],
  ["form", "📝 Анкета"],
  ["apps", "📋 Заявки"],
  ["pay", "💳 Оплата"],
  ["comms", "📢 Общение"],
  ["game", "🎮 Геймификация"],
  ["data", "📊 Данные"],
  ["manage", "🔧 Управление"],
];

// Навигация: вкладка показывается, если раздел включён чекбоксом (miniapp_section_*) И
// человек имеет к нему доступ: делегатские — при is_delegate, менеджерские — по caps.
// `group` — раздел из SECTION_GROUPS, под заголовком которого плитка встаёт в хабе менеджера
// (hub.js). У делегатских записей его нет: делегатский хаб не группируется (D-05). Поле лишнее
// для visibleNav/homeHash/navLabel и раскладок A/B — они его игнорируют.
export const NAV = [
  { hash: "#/tasks", section: "tasks", delegate: true },
  { hash: "#/coins", section: "coins", delegate: true },
  { hash: "#/leaderboard", section: "leaderboard", delegate: true },
  { hash: "#/profile", section: "profile", delegate: true },
  { hash: "#/review", section: "review", cap: "moderate_game", group: "game" },
  { hash: "#/admin-tasks", section: "admin_tasks", cap: "moderate_game", group: "game" },
  { hash: "#/admin-coins", section: "coins", cap: "moderate_game", staffOnly: true, group: "game" },
  { hash: "#/stats", section: "stats", cap: "stats", group: "data" },
  { hash: "#/settings", section: "settings", cap: "settings", group: "manage" },
];

// Иконки навигации (D-13) по маршруту — общий словарь для таб-бара (раскладка A) и плиток
// хаба (screens/hub.js), чтобы иконка раздела не расходилась между раскладками.
export const NAV_ICONS = {
  "#/tasks": "target",
  "#/coins": "coins",
  "#/leaderboard": "trophy",
  "#/profile": "user",
  "#/review": "check-circle-2",
  "#/admin-tasks": "clipboard-list",
  "#/admin-coins": "wallet",
  "#/stats": "bar-chart-2",
  "#/settings": "settings",
};

const compiled = ROUTES.map(([pattern, module]) => {
  const names = [];
  const source = pattern
    .slice(1)
    .replace(/[.*+?^$()|[\]\\]/g, "\\$&")
    .replace(/\{(\w+)\}/g, (_, name) => {
      names.push(name);
      return "([^/]+)";
    });
  return { regex: new RegExp(`^${source}$`), module, names };
});

// ── DOM-помощник: h(tag, attrs, ...children) — текст только через textContent ────────────
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function clear(el) {
  el.replaceChildren();
}

// ── Telegram SDK bootstrap ───────────────────────────────────────────────────────────────
function applySafeArea() {
  if (!tg) return;
  const inset = tg.contentSafeAreaInset || {};
  const safe = tg.safeAreaInset || {};
  root.style.setProperty("--tg-safe-top", `${(inset.top || 0) + (safe.top || 0)}px`);
  root.style.setProperty("--tg-safe-bottom", `${(inset.bottom || 0) + (safe.bottom || 0)}px`);
}

function applyTheme() {
  if (!tg) return;
  if (tg.colorScheme === "dark") {
    root.dataset.theme = "dark";
    const params = tg.themeParams || {};
    // Только bg/text из themeParams (D-02); остальные токены — из :root[data-theme="dark"].
    if (params.bg_color) root.style.setProperty("--bg", params.bg_color);
    if (params.text_color) root.style.setProperty("--text", params.text_color);
  } else {
    delete root.dataset.theme;
    root.style.removeProperty("--bg");
    root.style.removeProperty("--text");
  }
}

function backToHistory() {
  history.back();
}

function bootstrapTelegram() {
  applyMotionTier(); // до первой отрисовки (D-17); работает и без tg (браузерный fallback D-05)
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (typeof tg.disableVerticalSwipes === "function") tg.disableVerticalSwipes();
  } catch (_) { /* старый клиент без части методов — не критично */ }
  applyTheme();
  applySafeArea();
  if (tg.onEvent) {
    tg.onEvent("themeChanged", applyTheme);
    tg.onEvent("contentSafeAreaChanged", applySafeArea);
    tg.onEvent("safeAreaChanged", applySafeArea);
  }
  if (tg.BackButton) tg.BackButton.onClick(backToHistory);
}

// ── MainButton: принадлежит текущему экрану ─────────────────────────────────────────────
let mainButtonHandler = null;

function setMainButton(text, onClick, options = {}) {
  if (!tg || !tg.MainButton) return;
  const button = tg.MainButton;
  if (mainButtonHandler) button.offClick(mainButtonHandler);
  mainButtonHandler = null;
  if (!text || !onClick) {
    button.hide();
    return;
  }
  mainButtonHandler = onClick;
  button.setText(text);
  button.onClick(onClick);
  if (options.disabled) button.disable(); else button.enable();
  button.show();
}

function updateBackButton(hash) {
  if (!tg || !tg.BackButton) return;
  const isHome = !hash || hash === "#/" || hash === homeHash();
  if (isHome) tg.BackButton.hide(); else tg.BackButton.show();
}

// ── экраны состояний ─────────────────────────────────────────────────────────────────────
// D-13 (найдено планом 19.1-08 при визуальной сверке): эмодзи здесь были функциональной
// UI-иконкой, не голосом текста реестра ("эмодзи только в текстах реестра") -- заменены на
// линейные SVG из icons.js, тем же приёмом, что таб-бар/плитки хаба (NAV_ICONS). Копия
// STATE_TITLES/текстов НЕ переписана — UI-SPEC явно оставляет её вне этой фазы, в отличие от
// визуального рестайла самой карточки `.state`, который в границах.
const STATE_ICONS = { "open-in-bot": "link", expired: "clock", "no-access": "x", disabled: "alert-triangle", missing: "alert-triangle" };
const STATE_TITLES = {
  "open-in-bot": "Откройте через бота",
  expired: "Сессия истекла",
  "no-access": "Нет доступа",
  disabled: "Приложение выключено",
  missing: "Раздел пока недоступен",
};

let terminal = false; // после терминального экрана роутер больше не рисует экраны

export function showState(state, detail) {
  setMainButton(null);
  clear(navEl);
  const card = h("section", { class: "state", "data-state": state },
    h("div", { class: "icon" }, icon(STATE_ICONS[state] || "alert-triangle")),
    h("h1", { text: STATE_TITLES[state] || "" }),
  );

  if (state === "open-in-bot") {
    card.append(h("p", { text: ds.openInBotText }));
    const actions = h("div", { class: "actions" });
    if (ds.deepLink) {
      actions.append(h("a", { class: "btn", href: ds.deepLink, text: "Открыть в Telegram" }));
    }
    // Запасной вход менеджера (D-05): Login Widget дашборда. Показывается всегда — браузер
    // не знает, кто перед ним. Адрес относительный и не берётся из данных (T-19-75).
    // /auth/callback вернёт на корень дашборда (/), а не в /app — об этом подсказка.
    actions.append(h("a", { class: "btn secondary", href: "/login", text: ds.loginButton }));
    card.append(actions);
    card.append(h("p", { class: "hint", text: ds.loginHint }));
    terminal = true;
  } else if (state === "expired") {
    card.append(h("p", { text: ds.sessionExpiredText }));
    if (tg) {
      card.append(h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", text: "Закрыть", onClick: () => tg.close() }),
      ));
    }
    terminal = true;
  } else if (state === "no-access") {
    card.append(h("p", { text: ds.noAccessText }));
    if (detail) card.append(h("p", { class: "faint", text: detail }));
    card.append(h("div", { class: "actions" },
      h("a", { class: "btn ghost", href: homeHash(), text: "На главную" }),
    ));
  } else if (state === "disabled") {
    card.append(h("p", { text: ds.disabledText }));
    terminal = true;
  } else if (state === "missing") {
    card.append(h("p", { text: "Этот экран ещё не подключён. Всё то же самое есть в боте." }));
    card.append(h("div", { class: "actions" },
      h("a", { class: "btn ghost", href: homeHash(), text: "На главную" }),
    ));
  }

  clear(screenEl);
  screenEl.append(card);
}

// ── навигация ────────────────────────────────────────────────────────────────────────────
let me = null;
let sectionLabels = {};

export function visibleNav() {
  if (!me) return [];
  return NAV.filter((item) => {
    if (!me.sections || !me.sections[item.section]) return false;
    if (item.delegate) return Boolean(me.is_delegate);
    if (item.staffOnly && me.is_delegate) return false;
    return Array.isArray(me.caps) && me.caps.includes(item.cap);
  });
}

function homeHash() {
  const items = visibleNav();
  if (NAV_LAYOUT === "hub") return items.length ? "#/hub" : "#/";
  return items.length ? items[0].hash : "#/";
}

function isActive(activeHash, hash) {
  return activeHash === hash || activeHash.startsWith(`${hash}/`);
}

function navLabel(item) {
  return sectionLabels[item.section] || item.section;
}

// «Ещё» (раскладка A, nav.html:186-192) — оверлей, не маршрут: закрывается тапом вне листа
// и Telegram BackButton'ом, на серверную часть никак не влияет.
let closeOverflowSheet = null;

function closeOverflowSheetIfOpen() {
  if (closeOverflowSheet) closeOverflowSheet();
}

function openOverflowSheet(items, activeHash) {
  closeOverflowSheetIfOpen();
  const backdrop = h("div", { class: "sheet-backdrop" });
  const sheet = h("div", { class: "sheet", role: "dialog", "aria-label": "Ещё разделы" });
  function close() {
    backdrop.remove();
    if (tg && tg.BackButton && backHandler) {
      tg.BackButton.offClick(backHandler);
      tg.BackButton.onClick(backToHistory);
      updateBackButton(location.hash);
    }
    closeOverflowSheet = null;
  }
  for (const item of items) {
    sheet.append(h("a", {
      href: item.hash,
      class: `sheet-item ${isActive(activeHash, item.hash) ? "active" : ""}`,
      onClick: close,
    },
      icon(NAV_ICONS[item.hash] || "chevron-right"),
      h("span", { text: navLabel(item) }),
    ));
  }
  backdrop.append(sheet);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
  document.body.append(backdrop);

  let backHandler = null;
  if (tg && tg.BackButton) {
    backHandler = close;
    tg.BackButton.offClick(backToHistory);
    tg.BackButton.onClick(backHandler);
    tg.BackButton.show();
  }
  closeOverflowSheet = close;
}

const TABBAR_MAX = 4;

// Раскладка A: нижний таб-бар из максимум четырёх позиций + «Ещё» (nav.html:186-192).
function renderTabbar(activeHash) {
  const items = visibleNav();
  const overflow = items.length > TABBAR_MAX ? items.slice(TABBAR_MAX - 1) : [];
  const primary = overflow.length ? items.slice(0, TABBAR_MAX - 1) : items;
  const bar = h("div", { class: "tabbar" });
  for (const item of primary) {
    bar.append(h("a", {
      href: item.hash,
      class: `tab ${isActive(activeHash, item.hash) ? "on" : ""}`,
    },
      icon(NAV_ICONS[item.hash] || "chevron-right"),
      h("span", { text: navLabel(item) }),
    ));
  }
  if (overflow.length) {
    const overflowActive = overflow.some((item) => isActive(activeHash, item.hash));
    bar.append(h("button", {
      type: "button",
      class: `tab more ${overflowActive ? "on" : ""}`,
      onClick: () => openOverflowSheet(overflow, activeHash),
    },
      icon("more-horizontal"),
      h("span", { text: "Ещё" }),
    ));
  }
  navEl.append(bar);
}

// Раскладка B: верхние табы, активная скроллится в зону видимости (nav.html:196-199).
function renderToptabs(activeHash) {
  const bar = h("div", { class: "toptabs" });
  for (const item of visibleNav()) {
    bar.append(h("a", {
      href: item.hash,
      class: isActive(activeHash, item.hash) ? "on" : null,
      text: navLabel(item),
    }));
  }
  navEl.append(bar);
  const activeEl = bar.querySelector(".on");
  if (activeEl) {
    activeEl.scrollIntoView({
      behavior: root.dataset.motion === "off" ? "auto" : "smooth",
      inline: "center",
      block: "nearest",
    });
  }
}

// Диспетчер раскладки (D-10) — единственное место, зависящее от NAV_LAYOUT; источник пунктов
// у всех трёх раскладок один и тот же — visibleNav() поверх NAV и прав.
function renderNav(activeHash) {
  clear(navEl);
  closeOverflowSheetIfOpen();
  if (NAV_LAYOUT === "tabbar") renderTabbar(activeHash);
  else if (NAV_LAYOUT === "toptabs") renderToptabs(activeHash);
  // "hub" (C) — панель навигации не рисуется вовсе: разделы живут плитками на #/hub.
}

// ── роутер ───────────────────────────────────────────────────────────────────────────────
let current = null; // { module, unmount }

function match(hash) {
  const path = (hash || "").replace(/^#/, "") || "/";
  for (const route of compiled) {
    const found = route.regex.exec(path);
    if (!found) continue;
    const params = {};
    route.names.forEach((name, i) => { params[name] = decodeURIComponent(found[i + 1]); });
    return { module: route.module, params };
  }
  return null;
}

function navigate(hash) {
  location.hash = hash;
}

async function route() {
  if (terminal) return;
  let hash = location.hash;
  // Telegram Web (K/A) открывает вебвью с фрагментом собственных параметров
  // (#tgWebAppData=…&tgWebAppVersion=…) — это не наш маршрут, а транспорт initData.
  // Без этой ветки каждый первый вход из Telegram Web попадал в showState("missing")
  // («Раздел пока недоступен») вместо хаба — находка живой приёмки 19-10.
  if (hash.startsWith("#tgWebApp")) hash = "";
  if (!hash || hash === "#/" || hash === "#") {
    hash = homeHash();
    if (hash === "#/") {
      renderNav("");
      showState("no-access");
      return;
    }
    location.replace(hash);
    return;
  }

  const target = match(hash);
  renderNav(hash);
  updateBackButton(hash);
  setMainButton(null);
  if (current && typeof current.unmount === "function") {
    try { current.unmount(); } catch (_) { /* экран сам виноват */ }
  }
  current = null;

  if (!target) {
    showState("missing");
    return;
  }

  clear(screenEl);
  screenEl.append(h("div", { class: "loading", text: "Загрузка…" }));

  let mod;
  try {
    mod = await import(`./${target.module}`);
  } catch (_) {
    // Файл экрана ещё не положен (планы фазы добавляют их постепенно) — не белый лист.
    showState("missing");
    return;
  }
  if (location.hash !== hash) return; // пока грузили — ушли дальше
  if (!mod || typeof mod.render !== "function") {
    showState("missing");
    return;
  }
  current = { module: target.module, unmount: mod.unmount };
  clear(screenEl);
  try {
    await mod.render(screenEl, target.params, { me, tg, h, esc, setMainButton, navigate, showState, api });
  } catch (err) {
    if (err instanceof ApiError) return; // api() уже показал экран состояния (если нужно)
    clear(screenEl);
    screenEl.append(h("div", { class: "error-inline", text: "Не получилось открыть экран. Попробуйте ещё раз." }));
  }
}

// ── старт ────────────────────────────────────────────────────────────────────────────────
async function start() {
  bootstrapTelegram();
  setAuthErrorHandler((state, payload) => {
    const detail = payload && payload.kind ? `(${payload.kind})` : null;
    showState(state, state === "no-access" ? detail : null);
  });
  try {
    sectionLabels = JSON.parse(ds.sectionLabels || "{}");
  } catch (_) {
    sectionLabels = {};
  }

  // Без initData всё равно спрашиваем /me: у менеджера в браузере может быть cookie
  // дашборда. 401 no_auth приведёт на «Откройте через бота» через обработчик api().
  try {
    me = await api("/me");
  } catch (err) {
    if (!(err instanceof ApiError) || err.status >= 500 && err.reason !== "miniapp_off") {
      showState("no-access", "Сервер не ответил — попробуйте позже");
    }
    return;
  }
  // Бренд-паттерн hero (D-17/D-04): класс на <body>, CSS сам гейтит motion "full" внутри.
  body.classList.toggle("pattern-enabled", Boolean(me.pattern_enabled));
  window.addEventListener("hashchange", route);
  await route();
}

start();
