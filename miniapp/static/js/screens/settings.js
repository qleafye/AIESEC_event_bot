// Экран «⚙️ Настройки» (фаза 22, D-01…D-15; Phase 22 Plan 07, D-16: пересмотр владельца
// 03.09 — НЕ одна длинная страница). Два маршрута одного модуля:
//   #/settings        — стартовый экран: общий поиск по ВСЕМ настройкам сразу + два ряда
//                        плиток разделов («Нужно менеджеру» / «Реже», `sections[].tier`
//                        считает `settings_ops.SETTINGS_MAIN_SECTIONS`, JS ничего не решает
//                        сам — T-19-45, код раздела человеку не показывается, только подпись).
//   #/settings/{code}  — страница одного раздела: заголовок раздела + назад (Telegram
//                        BackButton, тот же общий механизм app.js, что у #/task/{id} — экран
//                        не рисует свою кнопку «назад»), группы/строки настроек раздела,
//                        гибридное сохранение (тумблер — сразу, остальное — пакетом с diff/
//                        confirm/stale).
//
// Экран — ПОТРЕБИТЕЛЬ `form.js` (план 22-03): ни одного собственного контрола по типу поля,
// ни одной надписи литералом — все тексты идут из `texts` ответа `GET .../settings/all`
// (реестр `miniapp_settings_*`). Заголовки экрана/раздела — из `document.body.dataset.
// sectionLabels`/`section.label` (тот же приём, что screens/hub.js::sectionLabelsFromDom), а
// не литералом.
//
// D-08/D-09 (гибридное сохранение, «изменено в боте» stale-сверка) переживают переход между
// разделами: `pending`/`fileNames`/`originalItems` — МОДУЛЬНЫЕ карты (не пересоздаются в
// render()), тот же приём, что `activeScrollHandler`/`activeDiffCleanup` ниже — правка,
// начатая на одном разделе и не сохранённая, не теряется при переходе на другой раздел или
// на стартовый экран назад; плавающая панель «Сохранить N изменений» на странице раздела
// показывает ОБЩЕЕ число несохранённых правок по всем разделам сразу и одним batch сохраняет
// все разом (сервер уже давно это умеет — `settings/batch` не завязан на «текущий» раздел).

import { sectionTitle, emptyState, errorState, labelText, tile } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";
import {
  field, setFieldState, settingSpec, confirmBox,
  searchFilter, highlightMatch, suggestTerms,
  errorText, isAuthError as isAuthErrorBase,
} from "../form.js";

// "not_editable" — единственная причина 403 у этого экрана, которая НЕ гейт авторизации
// (перенос конвенции task_edit.js/старого settings.js — план 21-04).
const AUTH_EXCEPT_REASONS = ["not_editable"];
function isAuthError(err) {
  return isAuthErrorBase(err, AUTH_EXCEPT_REASONS);
}

// Иконка плитки раздела (Lucide, план 19.1-04 инвентарь) — сопоставление в JS, а не с
// сервера: код раздела уже приходит (`section.token`), заводить ещё одно API-поле под
// единственный выбор из семи готовых иконок — лишний контракт (наименьшее из решений,
// допущенных Claude's Discretion 22-CONTEXT). Раздел без записи (новый токен в будущем)
// остаётся без иконки — плитка всё равно рабочая, просто без левой картинки.
const SECTION_ICONS = {
  event: "calendar",
  form: "clipboard-list",
  apps: "check-circle-2",
  pay: "wallet",
  game: "trophy",
  data: "bar-chart-2",
  manage: "settings",
};

// Свёртка групп переживает перезаход (WEB-SET-02, UI-SPEC «Каркас страницы» п.4) — тот же
// приём try/catch, что HUB_MODE_KEY/ONBOARDING_KEY в screens/hub.js: недоступный localStorage
// (приватный режим) не блокирует экран, просто каждый заход отрисовывает свёртку по умолчанию.
const COLLAPSE_PREFIX = "aiesec_miniapp_settings_collapsed_v1:";

function loadCollapsed(token) {
  try {
    const v = localStorage.getItem(COLLAPSE_PREFIX + token);
    return v === "1" ? true : (v === "0" ? false : null);
  } catch (_) {
    return null;
  }
}

function saveCollapsed(token, collapsed) {
  try {
    localStorage.setItem(COLLAPSE_PREFIX + token, collapsed ? "1" : "0");
  } catch (_) { /* приватный режим/недоступный localStorage — деградация к умолчанию */ }
}

// Подписи разделов — те же данные, что читает hub.js (data-атрибут шаблона, не литерал).
function sectionLabelsFromDom() {
  try {
    return JSON.parse(document.body.dataset.sectionLabels || "{}");
  } catch (_) {
    return {};
  }
}

function defaultDisplayText(item) {
  const d = item.default;
  if (d == null) return "";
  if (Array.isArray(d)) return d.join(", ");
  if (d === true) return "on";
  if (d === false) return "off";
  return String(d);
}

// Значение контрола (может быть массивом/числом/файлом) -> строка для POST settings/batch
// (сервер типизирует сам, `_parse_setting`/`validate_setting_value`, тот же приём, что бот).
function toBatchValue(v) {
  if (v == null) return null;
  if (Array.isArray(v)) return v.join(";");
  return String(v);
}

// «Станет» в diff-строке (WEB-SET-03) — человеческий предпросмотр локально изменённого
// значения без похода на сервер: enum/список хранят уже человекочитаемые опции (D-05),
// сброс (v === null) показывает дефолт реестра.
function humanDisplayValue(item, v, fileNames) {
  if (v == null) return defaultDisplayText(item);
  if (Array.isArray(v)) return v.join(", ");
  if ((item.type === "photo" || item.type === "file") && fileNames && fileNames.has(item.key)) {
    return fileNames.get(item.key);
  }
  return String(v);
}

// Маркер состояния строки (UI-SPEC §Color/Typography): per-city приоритетнее default/set/
// not-set (D-03/D-04). `item.key !== item.base_key` — признак композитного ключа выбранного
// города (per_city_key), сервер отдаёт его только когда шапка города указывает конкретный
// город (роутер miniapp/routers/settings.py::_item_for).
function markerFor(item, texts) {
  const isComposite = item.key !== item.base_key;
  if (isComposite) {
    return item.is_city_override
      ? { text: texts.miniapp_settings_city_own_badge_text || "", cls: "is-set" }
      : { text: texts.miniapp_settings_city_default_badge_text || "", cls: "is-default" };
  }
  if (item.per_city && item.city_override_count > 0) {
    return {
      text: (texts.miniapp_settings_city_override_count_text || "").replace("{count}", String(item.city_override_count)),
      cls: "is-set",
      overrideLabels: item.city_override_labels || [],
    };
  }
  const empty = item.display === "" || item.display == null;
  if (item.is_default) {
    return {
      text: empty ? (texts.miniapp_settings_value_not_set_text || "") : (texts.miniapp_settings_value_default_text || ""),
      cls: "is-default",
    };
  }
  return { text: texts.miniapp_settings_value_set_text || "", cls: "is-set" };
}

// ── состояние сессии (D-08/D-09 «переживает переход между разделами», см. докстринг файла) ──
// МОДУЛЬНЫЕ карты — не пересоздаются в render(), живут, пока не сохранены/отменены или пока
// не перезагрузится страница целиком (ESM-модуль переживает переход между хэшами, как и
// activeScrollHandler/activeDiffCleanup ниже — тот же приём).
const pending = new Map(); // ключ -> значение с контрола (ещё не строка для сети, D-08)
const fileNames = new Map(); // key -> имя выбранного файла (для diff-предпросмотра photo/file)
const originalItems = new Map(); // key -> последний ПОДТВЕРЖДЁННЫЙ сервером item — источник
// правды для diff «было», base батча и repaint; накапливается по мере посещения разделов, не
// очищается при уходе с раздела — иначе diff/stale по ключу с ДРУГОГО раздела не нашёл бы item.
function setOriginal(item) { originalItems.set(item.key, item); }

// Слушатели живут вне замыкания render() (модульные переменные), чтобы unmount() мог снять
// именно те, что завёл последний render() (паттерн card.js/review.js).
let activeScrollHandler = null;
let activeDiffCleanup = null;

export async function render(root, params, ctx) {
  if (params && params.code) return renderSection(root, params.code, ctx);
  return renderStart(root, ctx);
}

// ══ Стартовый экран: общий поиск + плитки разделов (D-16) ════════════════════════════════

function sectionItemsFlat(section) {
  const out = [...section.toggles];
  for (const group of section.groups) {
    out.push(...group.items);
    // D-17 Task 3: party/short трек-композиты матрицы — свои ключи (не входят в group.items,
    // у них нет item_spec с сервера), но должны находиться поиском и считаться в точке
    // pending на плитке раздела наравне с обычными настройками (докстринг файла).
    if (group.matrix) {
      for (const row of group.matrix.rows) out.push(
        { key: row.party.key, label: row.label, help: "" },
        { key: row.short.key, label: row.label, help: "" },
      );
    }
  }
  return out;
}

function sectionItemCount(section) {
  return section.toggles.length + section.groups.reduce((n, g) => n + g.items.length, 0);
}

async function renderStart(root, ctx) {
  const { h, api, me, navigate } = ctx;

  let texts = {};
  let payload = null;

  const title = h("h1", {});
  const searchInput = h("input", {
    class: "input", type: "search", disabled: true,
    onInput: () => onSearchInput(),
  });
  const searchClear = h("button", {
    class: "settings-search-clear hidden", type: "button",
    onClick: () => { searchInput.value = ""; onSearchInput(); searchInput.focus(); },
  }, icon("x"));
  const searchField = h("div", { class: "search-field" }, icon("search"), searchInput, searchClear);
  const searchCount = h("p", { class: "settings-search-count", "aria-live": "polite" });
  const searchEmpty = h("div", { class: "settings-search-empty hidden" });
  const searchBar = h("div", { class: "settings-search" }, searchField, searchCount, searchEmpty);
  const stateWrap = h("div");
  const resultsWrap = h("div", { class: "hidden" });
  const tilesWrap = h("div");

  root.append(title, stateWrap, searchBar, resultsWrap, tilesWrap);

  title.textContent = labelText(sectionLabelsFromDom().settings || "");

  if (activeScrollHandler) window.removeEventListener("scroll", activeScrollHandler);
  if (activeDiffCleanup) { activeDiffCleanup(); activeDiffCleanup = null; }
  function onScroll() {
    searchBar.classList.toggle("scrolled", window.scrollY > 0);
  }
  activeScrollHandler = onScroll;
  window.addEventListener("scroll", onScroll, { passive: true });

  function renderSkeleton() {
    const bars = Array.from({ length: 6 }, () => {
      const bar = h("div", { class: "card" });
      bar.style.height = "48px";
      bar.style.opacity = ".5";
      return bar;
    });
    stateWrap.replaceChildren(...bars);
  }

  function clearState() {
    stateWrap.replaceChildren();
  }

  // ── ряды плиток разделов (D-16): «Нужно менеджеру» (tier=main) / «Реже» (tier=rare). ─────
  function sectionHasPendingDot(section) {
    if (!pending.size) return false;
    const keys = new Set(sectionItemsFlat(section).map((i) => i.key));
    for (const key of pending.keys()) if (keys.has(key)) return true;
    return false;
  }

  function buildTileRow(rowLabel, sections) {
    if (!sections.length) return null;
    const wrap = h("div", {});
    wrap.append(sectionTitle(h, rowLabel));
    const tiles = h("div", { class: "tiles" });
    for (const section of sections) {
      tiles.append(tile(h, {
        onClick: () => navigate(`#/settings/${section.token}`),
        iconName: SECTION_ICONS[section.token],
        label: labelText(section.label),
        meta: (texts.miniapp_settings_tile_count_text || "").replace("{n}", String(sectionItemCount(section))),
        dot: sectionHasPendingDot(section),
      }));
    }
    wrap.append(tiles);
    return wrap;
  }

  function renderTiles() {
    const main = payload.sections.filter((s) => s.tier === "main");
    const rare = payload.sections.filter((s) => s.tier !== "main");
    tilesWrap.replaceChildren(
      ...[
        buildTileRow(texts.miniapp_settings_row_main_label || "", main),
        buildTileRow(texts.miniapp_settings_row_rare_label || "", rare),
      ].filter(Boolean),
    );
  }

  // ── общий поиск (D-15, теперь по ВСЕМ разделам сразу) — результаты сгруппированы
  // подзаголовком раздела (T-19-45: подпись раздела, не код), тап по строке открывает раздел.
  function buildSearchRow(item, ranges, sectionToken) {
    const titleEl = h("div", { class: "flat-row-title" });
    titleEl.replaceChildren(...highlightMatch(h, item.label, ranges.label));
    const metaEl = item.help ? h("div", { class: "flat-row-meta" }) : null;
    if (metaEl) metaEl.replaceChildren(...highlightMatch(h, item.help, ranges.help));
    return h("button", {
      type: "button", class: "flat-row", "aria-label": item.label,
      onClick: () => navigate(`#/settings/${sectionToken}`),
    },
      h("div", { class: "flat-row-body" }, titleEl, metaEl),
      h("span", { class: "flat-row-chev" }, icon("chevron-right")),
    );
  }

  function allItems() {
    const out = [];
    for (const section of payload.sections) out.push(...sectionItemsFlat(section));
    return out;
  }

  function onSearchInput() {
    const query = searchInput.value;
    searchClear.classList.toggle("hidden", !query);

    if (!query) {
      resultsWrap.classList.add("hidden");
      tilesWrap.classList.remove("hidden");
      searchCount.textContent = "";
      searchCount.classList.remove("active");
      renderSearchEmpty(false, query);
      return;
    }

    const items = allItems();
    const results = searchFilter(items, query);
    const rangesByKey = new Map(results.map((r) => [r.item.key, r.ranges]));

    let shown = 0;
    const groups = [];
    for (const section of payload.sections) {
      const rows = [];
      for (const item of sectionItemsFlat(section)) {
        const ranges = rangesByKey.get(item.key);
        if (!ranges) continue;
        rows.push(buildSearchRow(item, ranges, section.token));
        shown += 1;
      }
      if (rows.length) groups.push(h("div", {}, sectionTitle(h, section.label), h("div", { class: "flat-list" }, ...rows)));
    }

    tilesWrap.classList.add("hidden");
    resultsWrap.classList.toggle("hidden", groups.length === 0);
    resultsWrap.replaceChildren(...groups);

    searchCount.textContent = (texts.miniapp_settings_search_count_text || "")
      .replace("{shown}", String(shown)).replace("{total}", String(payload.total));
    searchCount.classList.add("active");

    renderSearchEmpty(shown === 0, query, items);
  }

  function renderSearchEmpty(show, query, items) {
    searchEmpty.classList.toggle("hidden", !show);
    if (!show) return;
    const suggestions = suggestTerms(items || allItems(), query, 3);
    const template = texts.miniapp_settings_search_suggest_text || "";
    const parts = template.split("{suggestions}");
    const suggestRow = suggestions.length
      ? h("p", { class: "settings-search-suggest" },
          parts[0] || "",
          ...suggestions.map((w) => h("button", {
            class: "chip", type: "button", text: w,
            onClick: () => { searchInput.value = w; onSearchInput(); },
          })),
          parts[1] || "",
        )
      : null;
    searchEmpty.replaceChildren(
      ...[
        h("h2", { text: texts.miniapp_settings_search_empty_heading_text || "" }),
        h("p", { text: texts.miniapp_settings_search_empty_body_text || "" }),
        suggestRow,
      ].filter(Boolean),
    );
  }

  async function loadAndRender() {
    searchInput.disabled = true;
    clearState();
    renderSkeleton();
    let data;
    try {
      data = await api("/admin/settings/all");
    } catch (err) {
      clearState();
      if (isAuthError(err)) return;
      stateWrap.append(errorState(h, {
        me, text: errorText(err, texts.miniapp_settings_load_error_text || ""), retry: loadAndRender,
      }));
      return;
    }
    payload = data;
    texts = data.texts || {};
    clearState();
    searchInput.disabled = false;
    searchInput.placeholder = texts.miniapp_settings_search_placeholder_text || "";

    if (!data.sections.length) {
      tilesWrap.replaceChildren(emptyState(h, { me, text: texts.miniapp_settings_load_error_text || "" }));
      return;
    }
    renderTiles();
  }

  await loadAndRender();
}

// ══ Страница одного раздела: группы/строки + гибридное сохранение ════════════════════════

async function renderSection(root, code, ctx) {
  const { h, api, me } = ctx;

  // ── состояние экрана (per-render; pending/fileNames/originalItems — модульные, см. верх
  // файла) ─────────────────────────────────────────────────────────────────────────────────
  let texts = {};
  let payload = null;
  let section = null;
  const itemIndex = new Map(); // key -> { item, el } — ТЕКУЩЕЕ отображение этой страницы
  const groupCollapsedState = new Map(); // token -> bool
  // D-17 Task 3: ячейки матрицы «трек × вопрос» не проходят через buildRow()/itemIndex
  // (у них нет отдельной строки-field, три тумблера сидят в одной строке вопроса) — свой
  // маленький реестр key -> paint(value) перекрашивает только сам тумблер, updateBatchBar()
  // зовёт его вместе с обычной перерисовкой (единая точка «что-то в pending изменилось»).
  const matrixCellPaint = new Map();
  let busyToggle = false;
  let busyBatch = false;

  // Правка, начатая на другом разделе и ещё не сохранённая (module-scope pending), не должна
  // молча исчезать с глаз при возврате на её раздел — строка показывает именно ЧЕРНОВИК, не
  // забытое сервером значение (D-08/D-09, докстринг файла).
  function withPendingOverride(item) {
    if (!pending.has(item.key)) return item;
    const v = pending.get(item.key);
    return { ...item, value: v, display: humanDisplayValue(item, v, fileNames), is_default: v == null };
  }

  // ── статичный каркас ──────────────────────────────────────────────────────────────────
  const title = h("h1", {});
  const cityBar = h("div", { class: "choice-chips hidden" });
  const stateWrap = h("div"); // скелетон / ошибка загрузки
  const sectionsWrap = h("div");

  root.append(title, cityBar, stateWrap, sectionsWrap);

  if (activeScrollHandler) { window.removeEventListener("scroll", activeScrollHandler); activeScrollHandler = null; }
  if (activeDiffCleanup) { activeDiffCleanup(); activeDiffCleanup = null; }

  function renderSkeleton(withText) {
    const bars = Array.from({ length: 6 }, () => {
      const bar = h("div", { class: "card" });
      bar.style.height = "48px";
      bar.style.opacity = ".5";
      return bar;
    });
    stateWrap.replaceChildren(
      ...[withText && texts.miniapp_settings_loading_text ? h("p", { class: "muted", text: texts.miniapp_settings_loading_text }) : null, ...bars].filter(Boolean),
    );
  }

  function clearState() {
    stateWrap.replaceChildren();
  }

  // ── шапка города (D-04) — тот же set_admin_city, что у бота; переключение перечитывает
  // весь settings/all (значения/маркеры per-city зависят от выбранного города). ───────────
  function buildCityBar(cityHeader) {
    cityBar.replaceChildren();
    if (!cityHeader) {
      cityBar.classList.add("hidden");
      return;
    }
    cityBar.classList.remove("hidden");
    const choices = [];
    if (cityHeader.can_select_all) {
      choices.push({ code: cityHeader.all_cities, label: cityHeader.all_cities_label });
    }
    for (const c of cityHeader.cities) choices.push(c);
    for (const c of choices) {
      cityBar.append(h("button", {
        class: `chip-choice${cityHeader.selected === c.code ? " chosen" : ""}`,
        type: "button", text: c.label,
        onClick: () => onCityPick(c.code),
      }));
    }
  }

  let cityBusy = false;
  async function onCityPick(code) {
    if (cityBusy || (payload && payload.city_header && payload.city_header.selected === code)) return;
    cityBusy = true;
    try {
      await api("/admin/settings/city", { method: "POST", body: { code } });
      await loadAndRender();
    } catch (err) {
      if (!isAuthError(err)) showToast(errorText(err, texts.miniapp_settings_error_toast_text || ""), "warn");
    } finally {
      cityBusy = false;
    }
  }

  // ── тост ─────────────────────────────────────────────────────────────────────────────────
  const toast = h("div", { class: "settings-toast chip off" }, icon("check"), h("span", {}));
  let toastTimer = null;
  function showToast(text, kind) {
    if (!text) return;
    toast.className = `settings-toast chip ${kind === "warn" ? "warn" : "success"}`;
    toast.querySelector("span").textContent = text;
    toast.classList.remove("off");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.add("off"), 2400);
  }
  root.append(toast);

  // ── тумблер: сохраняется по касанию (D-08). Опасное направление — тот же confirmBox, что
  // сегодня, текст последствий — только с сервера (item.confirm_text, T-22-13). ────────────
  let dangerToggle = null;
  const dangerConfirm = confirmBox(h, {
    onConfirm: () => { if (dangerToggle) saveToggle(dangerToggle.item, dangerToggle.el, dangerToggle.next); },
  });
  root.append(dangerConfirm);

  function openDangerToggleConfirm(item, el, next) {
    dangerToggle = { item, el, next };
    dangerConfirm.querySelector("p").textContent = item.confirm_text || "";
    dangerConfirm.querySelector(".btn.danger").textContent = texts.miniapp_settings_diff_confirm_dangerous_cta_text || "";
    dangerConfirm.querySelector(".btn.ghost").textContent = texts.miniapp_settings_batch_discard_text || "";
    dangerConfirm.open();
  }

  // ── сброс к умолчанию / «как везде» (D-10) — очередь в общий пакет (value: null,
  // эквивалент «-» бота), локальный предпросмотр строки без похода на сервер. ───────────────
  let resetTarget = null;
  const resetConfirm = confirmBox(h, {
    onConfirm: () => { if (resetTarget) queueReset(resetTarget.item); },
  });
  root.append(resetConfirm);

  function openResetDefaultConfirm(item) {
    resetTarget = { item };
    resetConfirm.querySelector("p").textContent = (texts.miniapp_settings_reset_default_confirm_text || "")
      .replace("{default}", defaultDisplayText(item));
    resetConfirm.querySelector(".btn.danger").textContent = texts.miniapp_settings_reset_default_label_text || "";
    resetConfirm.querySelector(".btn.ghost").textContent = texts.miniapp_settings_batch_discard_text || "";
    resetConfirm.open();
  }

  function queueReset(item) {
    resetConfirm.close();
    pending.set(item.key, null);
    fileNames.delete(item.key);
    updateBatchBar();
    repaintRow({ ...item, value: null, raw: item.raw, display: defaultDisplayText(item), is_default: true });
  }

  function queueCityReset(item) {
    pending.set(item.key, null);
    fileNames.delete(item.key);
    updateBatchBar();
    repaintRow({ ...item, value: null, raw: item.raw, is_city_override: false, display: "" });
  }

  // Ключи с плейсхолдерами в тексте/дефолте — тот же список, что HTML/шаблонные ключи бота
  // (item.html), плюс любой текст, где встречается «{слово}» в подсказке/значении/дефолте.
  const PLACEHOLDER_RE = /\{[a-z_]+\}/;
  function hasPlaceholder(item) {
    const hay = `${item.display || ""} ${item.default == null ? "" : item.default} ${item.help || ""}`;
    return item.type !== "toggle" && (PLACEHOLDER_RE.test(hay) || Boolean(item.html));
  }

  function buildPreviewButton(item) {
    const heading = h("p", { class: "label-role", text: texts.miniapp_settings_preview_heading_text || "" });
    const body = h("p", {});
    const panel = h("div", { class: "settings-preview hidden" }, heading, body);
    let busy = false;
    const btn = h("button", {
      class: "btn ghost", type: "button", "aria-label": item.label,
      onClick: async () => {
        if (busy) return;
        busy = true;
        try {
          const current = pending.has(item.key) ? pending.get(item.key) : item.value;
          const q = `key=${encodeURIComponent(item.base_key)}&value=${encodeURIComponent(toBatchValue(current) || "")}`;
          const resp = await api(`/admin/settings/preview?${q}`);
          body.textContent = resp.text || "";
          panel.classList.remove("hidden");
        } catch (err) {
          if (!isAuthError(err)) showToast(errorText(err, texts.miniapp_settings_error_toast_text || ""), "warn");
        } finally {
          busy = false;
        }
      },
    }, icon("eye"), h("span", { text: texts.miniapp_settings_preview_button_text || "" }));
    return { btn, panel };
  }

  function applyActions(el, item) {
    el._actions.replaceChildren();
    const locked = item.editable === false;
    const isComposite = item.key !== item.base_key;
    const buttons = [];
    if (!isComposite && item.is_default === false && !locked) {
      buttons.push(h("button", {
        class: "btn ghost", type: "button", "aria-label": item.label,
        onClick: () => openResetDefaultConfirm(item),
      }, icon("rotate-ccw"), h("span", { text: texts.miniapp_settings_reset_default_label_text || "" })));
    }
    if (isComposite && item.is_city_override && !locked) {
      buttons.push(h("button", {
        class: "btn ghost", type: "button", "aria-label": item.label,
        onClick: () => queueCityReset(item),
      }, icon("rotate-ccw"), h("span", { text: texts.miniapp_settings_reset_city_label_text || "" })));
    }
    let previewPanel = null;
    if (hasPlaceholder(item) && !locked) {
      const preview = buildPreviewButton(item);
      buttons.push(preview.btn);
      previewPanel = preview.panel;
    }
    if (buttons.length) {
      el._actions.append(...buttons);
      el._actions.classList.remove("hidden");
    } else {
      el._actions.classList.add("hidden");
    }
    if (previewPanel) el.append(previewPanel);
  }

  // ── плавающая панель «Сохранить N изменений» (D-08) — N считает ВСЕ несохранённые правки
  // сессии (модульный pending), не только правки текущего раздела (докстринг файла). ────────
  const batchBtnText = h("span", {});
  const batchBtn = h("button", { class: "btn", type: "button", onClick: () => openDiffDialog() }, batchBtnText);
  const batchDiscard = h("button", { class: "btn ghost", type: "button", onClick: () => discardPending() });
  const batchBar = h("div", { class: "settings-batch-bar off" }, batchBtn, batchDiscard);
  const batchSpacer = h("div", { class: "settings-batch-spacer hidden" });
  root.append(batchSpacer, batchBar);

  function updateBatchBar() {
    const count = pending.size;
    batchBtnText.textContent = (texts.miniapp_settings_batch_bar_text || "").replace("{count}", String(count));
    batchDiscard.textContent = texts.miniapp_settings_batch_discard_text || "";
    batchBar.classList.toggle("off", count === 0);
    batchSpacer.classList.toggle("hidden", count === 0);
    repaintMatrixCells();
  }

  // Значение ячейки матрицы прямо сейчас (черновик, если есть, иначе последнее подтверждённое
  // сервером) — та же формула, что withPendingOverride() для обычных строк, без пересборки
  // всего item.
  function matrixCellValue(key) {
    if (pending.has(key)) return pending.get(key);
    const original = originalItems.get(key);
    return original ? original.value : null;
  }

  function repaintMatrixCells() {
    for (const [key, paint] of matrixCellPaint) paint(matrixCellValue(key));
  }

  function discardPending() {
    if (busyBatch) return;
    for (const key of pending.keys()) {
      const original = originalItems.get(key);
      if (original) repaintRow(original);
    }
    pending.clear();
    fileNames.clear();
    updateBatchBar();
  }

  // ── диалог diff (WEB-SET-03, D-08/D-09) — переиспользует .sheet-backdrop/.sheet (тот же
  // компонент, что «Ещё» app.js::openOverflowSheet). Порядок пунктов: обычные → опасные →
  // needs_confirm (Sheets) → stale, единым списком. Может показать правки С ДРУГИХ разделов
  // (originalItems накоплен по всем посещённым разделам сессии — см. докстринг файла). ──────
  const diffHeading = h("h2", {});
  const diffList = h("div", { class: "settings-diff" });
  const diffConfirmBtn = h("button", { class: "btn", type: "button", onClick: () => submitDiff() });
  const diffCancelBtn = h("button", { class: "btn ghost", type: "button", onClick: () => closeDiffDialog() });
  const diffSheet = h(
    "div", { class: "sheet settings-diff-sheet", role: "dialog", "aria-modal": "true", tabindex: "-1" },
    diffHeading, diffList, h("div", { class: "task-actions" }, diffConfirmBtn, diffCancelBtn),
  );
  const diffBackdrop = h("div", { class: "sheet-backdrop hidden" }, diffSheet);
  diffBackdrop.addEventListener("click", (e) => { if (e.target === diffBackdrop) closeDiffDialog(); });
  root.append(diffBackdrop);

  let confirmedKeys = new Set();
  const needsConfirmState = new Map(); // key -> текст сервера
  const staleState = new Map(); // key -> {raw, value}
  let batchErrors = new Map(); // key -> текст ошибки

  function onDiffKeydown(e) {
    if (e.key === "Escape") closeDiffDialog();
  }

  function diffCategoryFor(item) {
    return item && item.dangerous && item.confirm_text ? "dangerous" : "ordinary";
  }

  function openDiffDialog() {
    if (!pending.size) return;
    confirmedKeys = new Set(
      [...pending.keys()].filter((key) => diffCategoryFor(originalItems.get(key)) === "dangerous"),
    );
    needsConfirmState.clear();
    staleState.clear();
    batchErrors = new Map();
    renderDiffList();
    diffBackdrop.classList.remove("hidden");
    document.addEventListener("keydown", onDiffKeydown);
    diffSheet.focus();
  }

  function closeDiffDialog() {
    diffBackdrop.classList.add("hidden");
    document.removeEventListener("keydown", onDiffKeydown);
  }
  activeDiffCleanup = () => document.removeEventListener("keydown", onDiffKeydown);

  function setDiffBusy(busy) {
    diffConfirmBtn.disabled = busy;
    diffCancelBtn.disabled = busy;
    diffList.querySelectorAll("button").forEach((b) => { b.disabled = busy; });
  }

  function buildOrdinaryDiffRow(key, item, value) {
    const category = diffCategoryFor(item);
    const row = h("div", { class: `settings-diff-row${category === "dangerous" ? " dangerous" : ""}` });
    row.append(
      h("p", { class: "settings-diff-label", text: item.label }),
      h("p", { class: "settings-diff-was" }, `${texts.miniapp_settings_diff_was_label_text || ""}: ${item.display || ""}`),
      h("p", { class: "settings-diff-will" },
        `${texts.miniapp_settings_diff_will_label_text || ""}: `,
        h("span", { class: "settings-diff-value", text: humanDisplayValue(item, value, fileNames) }),
      ),
    );
    if (category === "dangerous") {
      row.append(h("p", { class: "settings-diff-note" }, icon("alert-triangle"), h("span", { text: item.confirm_text || "" })));
    }
    const err = batchErrors.get(key);
    if (err) row.append(h("p", { class: "settings-diff-note" }, icon("alert-triangle"), h("span", { text: err })));
    return row;
  }

  function buildNeedsConfirmRow(item, text) {
    return h("div", { class: "settings-diff-row dangerous" },
      h("p", { class: "settings-diff-label", text: item.label }),
      h("p", { class: "settings-diff-note" }, icon("alert-triangle"), h("span", { text })),
    );
  }

  function buildStaleRow(key, item, info) {
    return h("div", { class: "settings-diff-row stale" },
      h("p", { class: "settings-diff-label", text: item.label }),
      h("p", { class: "settings-diff-note" }, icon("alert-triangle"), h("span", { text: texts.miniapp_settings_stale_badge_text || "" })),
      h("p", { class: "settings-diff-was", text: (texts.miniapp_settings_stale_current_value_text || "").replace("{value}", info.value == null ? "" : String(info.value)) }),
      h("div", { class: "task-actions" },
        h("button", { class: "btn secondary", type: "button", text: texts.miniapp_settings_stale_overwrite_label_text || "", onClick: () => resolveStale(key, "overwrite") }),
        h("button", { class: "btn ghost", type: "button", text: texts.miniapp_settings_stale_keep_label_text || "", onClick: () => resolveStale(key, "keep") }),
      ),
    );
  }

  function resolveStale(key, choice) {
    const info = staleState.get(key) || {};
    staleState.delete(key);
    if (choice === "overwrite") {
      confirmedKeys.add(key);
    } else {
      pending.delete(key);
      fileNames.delete(key);
      const original = originalItems.get(key) || {};
      const fresh = {
        ...original, raw: info.raw, value: info.value,
        display: info.value == null ? "" : String(info.value),
        is_default: info.raw == null,
      };
      repaintRow(fresh);
      setOriginal(fresh);
      updateBatchBar();
    }
    if (!pending.size) { closeDiffDialog(); return; }
    renderDiffList();
  }

  function renderDiffList() {
    diffHeading.textContent = texts.miniapp_settings_diff_heading_text || "";
    const rows = [];
    for (const [key, value] of pending) {
      if (needsConfirmState.has(key) || staleState.has(key)) continue;
      rows.push(buildOrdinaryDiffRow(key, originalItems.get(key), value));
    }
    for (const [key, text] of needsConfirmState) rows.push(buildNeedsConfirmRow(originalItems.get(key), text));
    for (const [key, info] of staleState) rows.push(buildStaleRow(key, originalItems.get(key), info));
    diffList.replaceChildren(...rows);

    const hasWarn = [...pending.keys()].some((k) => diffCategoryFor(originalItems.get(k)) === "dangerous")
      || needsConfirmState.size > 0 || staleState.size > 0;
    diffConfirmBtn.textContent = hasWarn
      ? (texts.miniapp_settings_diff_confirm_dangerous_cta_text || "")
      : (texts.miniapp_settings_diff_confirm_cta_text || "");
    diffCancelBtn.textContent = texts.miniapp_settings_batch_discard_text || "";
  }

  async function submitDiff() {
    if (busyBatch || !pending.size) return;
    busyBatch = true;
    batchBar.classList.add("busy");
    setDiffBusy(true);
    try {
      const changes = [...pending.entries()].map(([key, value]) => ({ key, value: toBatchValue(value) }));
      const base = {};
      for (const key of pending.keys()) base[key] = (originalItems.get(key) || {}).raw ?? null;
      const resp = await api("/admin/settings/batch", { method: "POST", body: { changes, base, confirm: [...confirmedKeys] } });

      batchErrors = new Map(Object.entries(resp.errors || {}));
      for (const [key, text] of batchErrors) {
        const row = itemIndex.get(key);
        if (row) setFieldState(row.el, "error", { text });
      }

      needsConfirmState.clear();
      for (const entry of resp.needs_confirm || []) {
        needsConfirmState.set(entry.key, entry.text);
        confirmedKeys.add(entry.key);
      }

      staleState.clear();
      for (const entry of resp.stale || []) staleState.set(entry.key, entry);

      if (batchErrors.size || needsConfirmState.size || staleState.size) {
        renderDiffList();
        return;
      }

      for (const key of resp.saved || []) {
        // D-17 Task 3: трек-композиты (reg_q_X__party/__short) не приходят в resp.items
        // (сервер их пропускает — у них нет item_spec-обёртки, миниапп/routers/settings.py::
        // settings_batch), матрица красит ячейку сама сохранённым значением ДО того, как
        // pending его забудет — иначе после сохранения ячейка откатилась бы к значению,
        // которое видела ДО этой правки (matrixCellValue() читает pending, потом originalItems).
        if (matrixCellPaint.has(key)) {
          const prevOriginal = originalItems.get(key) || { key, base_key: key };
          originalItems.set(key, { ...prevOriginal, value: pending.get(key), raw: pending.get(key), is_default: false });
        }
        pending.delete(key);
        fileNames.delete(key);
      }
      for (const item of resp.items || []) { repaintRow(item); setOriginal(item); }
      updateBatchBar();
      closeDiffDialog();
      haptic("success");
      showToast(texts.miniapp_settings_saved_toast_text || "", "success");
    } catch (err) {
      if (!isAuthError(err)) showToast(errorText(err, texts.miniapp_settings_error_toast_text || ""), "warn");
    } finally {
      busyBatch = false;
      batchBar.classList.remove("busy");
      setDiffBusy(false);
    }
  }

  // ── строка настройки: field() из form.js + маркер состояния. ────────────────────────────
  function buildRow(rawItem) {
    const item = withPendingOverride(rawItem);
    const spec = settingSpec(item);
    const el = field(h, spec, item.value, (v) => onFieldChange(rawItem, el, v));
    el.classList.add("settings-row");
    el.dataset.key = item.key;

    const marker = h("button", { type: "button", class: "chip field-state", onClick: () => toggleOverrideList(el) });
    // D-17 Task 2 (владелец 03.09, «отступы кривые») — у тумблера (toggle И enum on/off,
    // D-17 Task 1) label скрыт (form.js::field), маркер в ЕГО пустом label-row повисал бы
    // орфанной строкой над тумблером. control тумблера (form.js::toggleControl) — САМ
    // `<button class="flat-row">` (D-08 клик по всей строке) — вложить туда ещё один
    // `<button>` (маркер) нельзя (HTML не допускает button-в-button, всплытие клика сломало бы
    // тумблер); вместо этого заворачиваем control+маркер СОСЕДЯМИ в один флекс-ряд — визуально
    // одна строка, кнопки остаются на одном уровне вложенности (app.css::.settings-row-toggle).
    const isToggleControl = spec.type === "toggle" && el._nodes.control instanceof HTMLElement
      && el._nodes.control.classList.contains("flat-row");
    if (isToggleControl) {
      const line = h("div", { class: "settings-row-toggle-line" });
      el._nodes.control.replaceWith(line);
      line.append(el._nodes.control, marker);
      el.classList.add("settings-row-toggle");
    } else {
      const labelRow = el._nodes.label ? el._nodes.label.parentElement : null;
      if (labelRow) labelRow.append(marker);
    }
    el._marker = marker;

    const overrideList = h("p", { class: "label-role hidden" });
    el.append(overrideList);
    el._overrideList = overrideList;

    const actions = h("div", { class: "field-actions hidden" });
    el.append(actions);
    el._actions = actions;

    if (item.editable === false) setFieldState(el, "disabled");
    applyMarker(el, item);
    applyActions(el, rawItem);
    paintHighlight(el, item);
    return { item: rawItem, el };
  }

  function toggleOverrideList(el) {
    if (!el._overrideLabelsText) return;
    el._overrideList.classList.toggle("hidden");
  }

  function applyMarker(el, item) {
    const m = markerFor(item, texts);
    el._marker.className = `chip field-state ${m.cls}`;
    el._marker.textContent = m.text;
    if (m.overrideLabels && m.overrideLabels.length) {
      el._overrideLabelsText = (texts.miniapp_settings_city_override_list_text || "").replace("{cities}", m.overrideLabels.join(", "));
      el._overrideList.textContent = el._overrideLabelsText;
    } else {
      el._overrideLabelsText = null;
      el._overrideList.classList.add("hidden");
      el._overrideList.textContent = "";
    }
  }

  function paintHighlight(el, item) {
    // D-17 Task 1: el.dataset.type — ОТРИСОВАННЫЙ тип (form.js::field ставит его из spec.type),
    // не сырой item.type реестра — у enum on/off (D-17) они расходятся (сервер отдаёт "enum",
    // рисуется тумблером), а title, который нужно перекрасить, живёт внутри .flat-row именно
    // у отрисованного тумблера.
    if (el.dataset.type === "toggle") {
      const titleEl = el._nodes.control && el._nodes.control.querySelector
        ? el._nodes.control.querySelector(".flat-row-title")
        : null;
      if (titleEl) titleEl.replaceChildren(...highlightMatch(h, item.label, []));
    } else if (el._nodes.label) {
      el._nodes.label.replaceChildren(...highlightMatch(h, item.label, []));
    }
    if (el._nodes.help) {
      el._nodes.help.replaceChildren(...highlightMatch(h, item.help || "", []));
    }
  }

  // Точечная перерисовка одной строки СВЕЖИМ item (после сохранения/отмены/stale-«оставить
  // как в боте») — заменяет DOM-узел строки, не трогая соседей. Если ключ принадлежит
  // ДРУГОМУ разделу (не показан на этой странице), itemIndex не находит prev — новый узел
  // просто не на что заменить, вызывающий код это переживает (см. resolveStale/submitDiff).
  function repaintRow(item) {
    const prev = itemIndex.get(item.key);
    const row = buildRow(item);
    itemIndex.set(item.key, row);
    if (prev && prev.el && prev.el.parentNode) prev.el.replaceWith(row.el);
    return row;
  }

  function onFieldChange(item, el, value) {
    if (item.type === "toggle") {
      // toggleControl(form.js) уже отдаёт СЛЕДУЮЩЕЕ значение ("on"/"off"), не текущее —
      // сохраняется сразу (D-08); опасное направление сначала подтверждается.
      if (item.confirm_text) openDangerToggleConfirm(item, el, value);
      else saveToggle(item, el, value);
      return;
    }
    if ((item.type === "photo" || item.type === "file") && typeof File !== "undefined" && value instanceof File) {
      handleFileUpload(item, el, value);
      return;
    }
    pending.set(item.key, value);
    updateBatchBar();
  }

  // ── фото/файл: тот же staff-путь POST /app/api/uploads, что резюме делегата. ─────────────
  let uploadLimitsPromise = null;
  function getUploadLimits() {
    if (!uploadLimitsPromise) uploadLimitsPromise = api("/uploads/limits").catch(() => ({}));
    return uploadLimitsPromise;
  }

  const IMAGE_EXT_RE = /\.(jpe?g|png|webp)$/i;

  async function handleFileUpload(item, el, file) {
    if (item.type === "photo" && !(file.type || "").startsWith("image/") && !IMAGE_EXT_RE.test(file.name || "")) {
      setFieldState(el, "error", { text: texts.miniapp_settings_upload_wrong_type_text || "" });
      return;
    }
    const limits = await getUploadLimits();
    if (limits.max_bytes && file.size > limits.max_bytes) {
      setFieldState(el, "error", { text: texts.miniapp_settings_upload_413_text || "" });
      return;
    }
    setFieldState(el, "uploading", { text: "" });
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const res = await api("/uploads", { method: "POST", form });
      fileNames.set(item.key, file.name);
      pending.set(item.key, res.content);
      updateBatchBar();
      setFieldState(el, "default");
    } catch (err) {
      if (isAuthError(err)) {
        setFieldState(el, "default");
        return;
      }
      let text = texts.miniapp_settings_error_toast_text || "";
      if (err && err.status === 413) text = texts.miniapp_settings_upload_413_text || "";
      else if (!err || typeof err.status !== "number") text = texts.miniapp_settings_upload_offline_text || "";
      else text = errorText(err, text);
      setFieldState(el, "error", { text });
    }
  }

  // ── тумблер: один POST batch из одного изменения (D-08). ────────────────────────────────
  async function saveToggle(item, el, nextValue) {
    if (busyToggle) return;
    busyToggle = true;
    dangerConfirm.close();
    const wasDangerous = Boolean(item.confirm_text);
    try {
      const resp = await api("/admin/settings/batch", { method: "POST", body: {
        changes: [{ key: item.key, value: nextValue }],
        base: { [item.key]: item.raw },
        confirm: wasDangerous ? [item.key] : [],
      } });
      if (resp.errors && resp.errors[item.key]) {
        if (el._nodes.control && el._nodes.control.paint) el._nodes.control.paint(item.value);
        setFieldState(el, "error", { text: resp.errors[item.key] });
        return;
      }
      if (resp.saved && resp.saved.includes(item.key) && resp.items && resp.items[0]) {
        const row = repaintRow(resp.items[0]); setOriginal(resp.items[0]);
        row.el.classList.add("is-flash");
        setTimeout(() => row.el.classList.remove("is-flash"), 400);
        haptic("success");
        showToast(
          wasDangerous ? texts.miniapp_settings_dangerous_saved_toast_text : texts.miniapp_settings_saved_toast_text,
          "success",
        );
        return;
      }
      await loadAndRender();
    } catch (err) {
      if (!isAuthError(err)) {
        if (el._nodes.control && el._nodes.control.paint) el._nodes.control.paint(item.value);
        setFieldState(el, "error", { text: errorText(err, texts.miniapp_settings_error_toast_text || "") });
      }
    } finally {
      busyToggle = false;
    }
  }

  // ── матрица «трек × вопрос» (D-17 Task 3, владелец 03.09: «одна строка на вопрос, три
  // маленьких тумблера вместо трёх отдельных строк на каждый»). Заменяет флат-список ТОЛЬКО
  // визуально — group.items (тот же список reg_q_*) остаётся в ответе для поиска (T-19-45,
  // sectionItemsFlat выше) и для diff/stale базовых значений ("полная" колонка — реальный
  // ключ реестра, её original уже пришёл в group.items). Party/short — трек-композиты без
  // отдельного item_spec (сервер их не рисует строкой), original им собирает сам экран.
  function matrixColumnLabel(track) {
    if (track === "party") return texts.miniapp_settings_reg_matrix_party_label_text || "";
    if (track === "short") return texts.miniapp_settings_reg_matrix_short_label_text || "";
    return texts.miniapp_settings_reg_matrix_full_label_text || "";
  }

  function matrixCellDisplay(value) {
    return value === "on" ? (texts.miniapp_settings_value_set_text || "") : (texts.miniapp_settings_value_not_set_text || "");
  }

  function buildMatrixToggle(row, track, cell) {
    const key = cell.key;
    if (track !== "full") {
      // party/short — синтетический original (нет отдельного item_spec с сервера): raw
      // отсутствует у унаследованного/неустановленного значения (тот же смысл, что raw=null
      // у обычной настройки «по умолчанию», stale-сверка submitDiff читает его наравне).
      originalItems.set(key, {
        key, base_key: key, type: "toggle",
        label: `${row.label} — ${matrixColumnLabel(track)}`,
        value: cell.value, raw: cell.is_inherited ? null : cell.value,
        display: matrixCellDisplay(cell.value), is_default: cell.is_inherited,
        dangerous: false, confirm_text: null,
      });
    }
    const btn = h("button", {
      type: "button", class: `settings-matrix-toggle settings-matrix-toggle-${track}`,
      "aria-label": `${row.label} — ${matrixColumnLabel(track)}`,
      "aria-pressed": "false",
      onClick: () => {
        const next = matrixCellValue(key) === "on" ? "off" : "on";
        pending.set(key, next);
        updateBatchBar();
      },
    }, icon("check"));
    function paint(value) {
      const on = value === "on";
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      const original = originalItems.get(key);
      const inherited = track !== "full" && Boolean(original && original.is_default) && !pending.has(key);
      btn.classList.toggle("is-inherited", inherited);
    }
    matrixCellPaint.set(key, paint);
    paint(matrixCellValue(key));
    return btn;
  }

  function buildMatrixRow(row) {
    return h("div", { class: "settings-matrix-row" },
      h("span", { class: "settings-matrix-row-label", text: row.label }),
      h("div", { class: "settings-matrix-row-toggles" },
        buildMatrixToggle(row, "full", row.full),
        buildMatrixToggle(row, "party", row.party),
        buildMatrixToggle(row, "short", row.short),
      ),
    );
  }

  function buildMatrix(group) {
    for (const item of group.items) setOriginal(item); // "полная" колонка — реальные reg_q_*
    return h("div", { class: "settings-matrix" },
      h("div", { class: "settings-matrix-head" },
        h("span", { class: "settings-matrix-head-question" }),
        h("span", { class: "settings-matrix-head-col", text: matrixColumnLabel("full") }),
        h("span", { class: "settings-matrix-head-col", text: matrixColumnLabel("party") }),
        h("span", { class: "settings-matrix-head-col", text: matrixColumnLabel("short") }),
      ),
      ...group.matrix.rows.map((row) => buildMatrixRow(row)),
    );
  }

  // ── карточка группы: заголовок-кнопка (счётчик + шеврон, вращение — CSS .collapsed) +
  // тело со строками (или матрицей — reg_questions, см. выше). ────────────────────────────
  function buildGroupCard(group, isFirst) {
    const rowsWrap = h("div", { class: "settings-group-body" });
    if (group.matrix) {
      rowsWrap.append(buildMatrix(group));
    } else {
      for (const item of group.items) {
        const row = buildRow(item);
        itemIndex.set(item.key, row);
        setOriginal(item);
        rowsWrap.append(row.el);
      }
    }
    const countEl = h("span", { class: "settings-group-count", text: String(group.items.length) });
    const chevron = h("span", { class: "settings-group-chevron" }, icon("chevron-down"));
    const wrap = h("div", { class: "settings-group" });
    wrap.dataset.token = group.token;
    const head = h("button", {
      class: "settings-group-head", type: "button", "aria-label": group.label, "aria-expanded": "true",
      onClick: () => onGroupHeadClick(group.token, wrap),
    }, h("span", { class: "settings-group-title", text: group.label }), countEl, chevron);
    wrap.append(head, rowsWrap);

    const stored = loadCollapsed(group.token);
    const collapsed = stored != null ? stored : !isFirst;
    groupCollapsedState.set(group.token, collapsed);
    wrap.classList.toggle("collapsed", collapsed);
    head.setAttribute("aria-expanded", collapsed ? "false" : "true");
    return wrap;
  }

  function onGroupHeadClick(token, wrap) {
    const next = !groupCollapsedState.get(token);
    groupCollapsedState.set(token, next);
    saveCollapsed(token, next);
    wrap.classList.toggle("collapsed", next);
    const head = wrap.querySelector(".settings-group-head");
    if (head) head.setAttribute("aria-expanded", next ? "false" : "true");
  }

  // ── тело раздела: тумблеры раздела (флат-список строк) + карточки групп. ─────────────────
  function renderSectionBody() {
    sectionsWrap.replaceChildren();
    if (section.toggles.length) {
      const toggleList = h("div", { class: "settings-toggle-list" });
      for (const item of section.toggles) {
        const row = buildRow(item);
        itemIndex.set(item.key, row);
        setOriginal(item);
        toggleList.append(row.el);
      }
      sectionsWrap.append(toggleList);
    }
    section.groups.forEach((group, idx) => {
      sectionsWrap.append(buildGroupCard(group, idx === 0 && !section.toggles.length));
    });
  }

  // ── загрузка/перезагрузка ────────────────────────────────────────────────────────────
  async function loadAndRender() {
    itemIndex.clear();
    groupCollapsedState.clear();
    clearState();
    renderSkeleton(Object.keys(texts).length > 0);
    let data;
    try {
      data = await api("/admin/settings/all");
    } catch (err) {
      clearState();
      if (isAuthError(err)) return;
      stateWrap.append(errorState(h, {
        me, text: errorText(err, texts.miniapp_settings_load_error_text || ""), retry: loadAndRender,
      }));
      return;
    }
    payload = data;
    texts = data.texts || {};
    section = data.sections.find((s) => s.token === code) || null;
    clearState();

    title.textContent = section ? labelText(section.label) : labelText(sectionLabelsFromDom().settings || "");

    if (!section) {
      sectionsWrap.replaceChildren(emptyState(h, { me, text: texts.miniapp_settings_load_error_text || "" }));
      cityBar.classList.add("hidden");
      return;
    }

    buildCityBar(data.city_header);
    renderSectionBody();
    updateBatchBar();
  }

  await loadAndRender();
}

export function unmount() {
  if (activeScrollHandler) {
    window.removeEventListener("scroll", activeScrollHandler);
    activeScrollHandler = null;
  }
  if (activeDiffCleanup) {
    activeDiffCleanup();
    activeDiffCleanup = null;
  }
}
