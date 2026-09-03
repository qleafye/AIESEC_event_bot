// Экран «⚙️ Настройки» (фаза 22, D-01…D-15, 22-UI-SPEC.md): весь правимый реестр
// `SETTINGS_SCHEMA` одной страницей — липкий поиск сверху, разделы Phase 20 с карточками
// групп бота, строки настроек на месте (не отдельный экран правки), гибридное сохранение
// (тумблер — сразу, остальное — пакетом с diff/confirm/stale). Экран — ПОТРЕБИТЕЛЬ `form.js`
// (план 22-03): ни одного собственного контрола по типу поля, ни одной надписи литералом —
// все тексты идут из `texts` ответа `GET .../settings/all` (реестр `miniapp_settings_*`,
// план 22-02). Заголовок экрана и подписи разделов — из `document.body.dataset.sectionLabels`
// (тот же приём, что `screens/hub.js::sectionLabelsFromDom`), а не литералом.
//
// Задача 1 (каркас): шапка города, липкий поиск, разделы/группы/строки, свёртка через
// localStorage. Задача 2 добавляет гибридное сохранение (тумблер сразу / батч с diff).
// Задача 3 — превью, сброс к дефолту, «как везде», фото/файл через staff-путь /uploads.
//
// Группа заголовка «N/M» — символьный счётчик (не фраза «N из M»): в реестре 22-02 есть
// текст для строки поиска (`search_count_text`), но не для заголовка карточки группы —
// отдельного ключа под эту фразу план не завёл, а вводить новый ключ реестра — вне границ
// файлов этого плана (только JS/CSS/тесты). Слэш — не человеческий текст, сторож литералов
// его не видит.

import { sectionTitle, emptyState, errorState } from "../ui.js";
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

// Слушатель скролла живёт вне замыкания render() (модульная переменная), чтобы unmount()
// мог снять именно его — без этого повторный заход на экран копил бы по листенеру на visit
// (паттерн card.js/review.js: unmount снимает то, что завёл render).
let activeScrollHandler = null;
let activeDiffCleanup = null;

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;

  // ── состояние экрана ────────────────────────────────────────────────────────────────
  let texts = {};
  let payload = null;
  const itemIndex = new Map(); // key -> { item, el } — ТЕКУЩЕЕ отображение (может быть
  // локальным предпросмотром сброса, ещё не сохранённым)
  const originalItems = new Map(); // key -> последний ПОДТВЕРЖДЁННЫЙ сервером item — источник
  // правды для diff «было», base батча и отмены правок; локальный предпросмотр (кнопка
  // «Сбросить»/«Как везде» — задача 3) его не трогает, чтобы «Отменить правки» не подменял
  // отмену настоящим значением сброса.
  function setOriginal(item) { originalItems.set(item.key, item); }
  const groupCollapsedState = new Map(); // token -> bool
  let query = "";

  // Гибридное сохранение (D-08): тумблер сохраняется сам по себе, отдельным POST batch из
  // одного изменения (busyToggle — глобальный однократный замок, «один тап = один запрос»).
  // Остальные типы копятся здесь локально (ключ -> значение с контрола, ЕЩЁ НЕ строка для
  // сети — toBatchValue() переводит в строку только перед отправкой/показом diff) до тапа на
  // плавающую панель «Сохранить N изменений». Своя карта вместо form.js::createFormState —
  // stale «оставить как в боте» требует точечного снятия ОДНОГО ключа из черновика, которого
  // API createFormState не даёт (только полный reset()); полноценно реализовать это здесь —
  // три метода Map, тянуть невписывающийся контракт в form.js внутри этого плана незачем.
  const pending = new Map();
  const fileNames = new Map(); // key -> имя выбранного файла (для diff-предпросмотра photo/file)
  let busyToggle = false;
  let busyBatch = false;

  // ── статичный каркас (строится один раз, содержимое заполняется после загрузки) ──────
  const title = h("h1", {});
  const cityBar = h("div", { class: "choice-chips hidden" });
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
  const sectionsWrap = h("div");
  const stateWrap = h("div"); // скелетон / ошибка загрузки

  root.append(title, cityBar, stateWrap, searchBar, sectionsWrap);

  title.textContent = sectionLabelsFromDom().settings || "";

  if (activeScrollHandler) window.removeEventListener("scroll", activeScrollHandler);
  if (activeDiffCleanup) { activeDiffCleanup(); activeDiffCleanup = null; }
  function onScroll() {
    searchBar.classList.toggle("scrolled", window.scrollY > 0);
  }
  activeScrollHandler = onScroll;
  window.addEventListener("scroll", onScroll, { passive: true });

  // ── скелетон загрузки (без текста на первом заходе — texts.miniapp_settings_loading_text
  // ещё не пришёл, это ЧАСТЬ того же ответа, который мы ждём; на повторных загрузках
  // (смена города) texts уже в кэше — см. loadAndRender). Только существующие классы —
  // .card/.muted, задача 1 не заводит новых CSS. ─────────────────────────────────────────
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

  // ── тост (используется задачей 2, каркас — здесь, чтобы Task 1 уже мог показать ошибку
  // переключения города человеческим текстом реестра). ────────────────────────────────────
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
  // сегодня (перенос конвенции старого settings.js), текст последствий — только с сервера
  // (item.confirm_text, T-22-13: клиент не решает, что опасно). Кнопки диалога переиспользуют
  // ближайшие по смыслу тексты реестра — под точечный «да, эта одна опасная правка» отдельного
  // ключа план 22-02 не завёл (только общий CTA батча и общий «отменить правки»).
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
  // эквивалент «-» бота), локальный предпросмотр строки без похода на сервер. Confirm нужен
  // только у сброса к дефолту (текст с {default} — менеджеру важно ЧТО именно вернётся);
  // «как везде» безопасно, без подтверждения — снимает только переопределение города.
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

  // ── плавающая панель «Сохранить N изменений» (D-08) — появляется при первом dirty
  // нетумблерном поле; MainButton-паттерн дублирования нет (batch — не MainButton задача:
  // здесь ДВЕ кнопки — сохранить/отменить, task_edit.js использует MainButton только когда
  // действие одно). ────────────────────────────────────────────────────────────────────────
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
  // компонент, что «Ещё» app.js::openOverflowSheet), не заводит нового CSS. Порядок пунктов
  // (A4 UI-SPEC): обычные → опасные → needs_confirm (Sheets) → stale, единым списком. ────────
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
  // Уход с экрана при открытом диалоге (переход по BackButton/ссылке) — снимаем keydown-
  // листенер вместе со скроллом (unmount снимает то, что завёл render, тот же приём выше).
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
      // «Оставить как в боте» — актуальное значение из ответа stale (info.raw/info.value),
      // не наш черновик и не то, что было при загрузке экрана (оно уже устарело — иначе
      // stale не случился бы). Это ПОДТВЕРЖДЁННАЯ сервером правда — обновляет и originalItems.
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

  // ── строка настройки: field() из form.js + маркер состояния (расширение .field, не новый
  // контейнер — settings-row добавляется прямо на узел field()). ─────────────────────────
  function buildRow(item) {
    const spec = settingSpec(item);
    const el = field(h, spec, item.value, (v) => onFieldChange(item, el, v));
    el.classList.add("settings-row");
    el.dataset.key = item.key;

    const marker = h("button", { type: "button", class: "chip field-state", onClick: () => toggleOverrideList(el) });
    const labelRow = el._nodes.label ? el._nodes.label.parentElement : null;
    if (labelRow) labelRow.append(marker);
    el._marker = marker;

    const overrideList = h("p", { class: "label-role hidden" });
    el.append(overrideList);
    el._overrideList = overrideList;

    const actions = h("div", { class: "field-actions hidden" });
    el.append(actions);
    el._actions = actions;

    if (item.editable === false) setFieldState(el, "disabled");
    applyMarker(el, item);
    applyActions(el, item);
    paintHighlight(el, item, null);
    return { item, el };
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

  function paintHighlight(el, item, ranges) {
    const labelRanges = ranges ? ranges.label : [];
    const helpRanges = ranges ? ranges.help : [];
    if (item.type === "toggle") {
      const titleEl = el._nodes.control && el._nodes.control.querySelector
        ? el._nodes.control.querySelector(".flat-row-title")
        : null;
      if (titleEl) titleEl.replaceChildren(...highlightMatch(h, item.label, labelRanges));
    } else if (el._nodes.label) {
      el._nodes.label.replaceChildren(...highlightMatch(h, item.label, labelRanges));
    }
    if (el._nodes.help) {
      el._nodes.help.replaceChildren(...highlightMatch(h, item.help || "", helpRanges));
    }
  }

  // Точечная перерисовка одной строки СВЕЖИМ item (после сохранения/отмены/stale-«оставить
  // как в боте») — вместо попытки обновить произвольный контрол на месте (у большинства
  // типов form.js нет универсального setValue, см. комментарий у `pending` выше). Заменяет
  // DOM-узел строки, не трогая соседей — фокус/скролл остальной страницы не прыгает.
  function repaintRow(item) {
    const prev = itemIndex.get(item.key);
    const row = buildRow(item);
    itemIndex.set(item.key, row);
    if (prev && prev.el && prev.el.parentNode) prev.el.replaceWith(row.el);
    if (query) paintHighlight(row.el, item, null);
    return row;
  }

  function onFieldChange(item, el, value) {
    if (item.type === "toggle") {
      // toggleControl(form.js) уже отдаёт СЛЕДУЮЩЕЕ значение ("on"/"off"), не текущее —
      // сохраняется сразу (D-08); опасное направление сначала подтверждается (item.confirm_text
      // — единственный источник опасности, T-22-13: клиент ничего не решает сам).
      if (item.confirm_text) openDangerToggleConfirm(item, el, value);
      else saveToggle(item, el, value);
      return;
    }
    if ((item.type === "photo" || item.type === "file") && typeof File !== "undefined" && value instanceof File) {
      // Дропзона (form.js::fileControl) уже нарисовала локальный предпросмотр сама (внутренний
      // input-листенер вызывает свой paint() до этого onChange) — здесь только загрузка через
      // существующий staff-путь /uploads (без нового транспорта, D-05) и постановка file_id в
      // общий пакет; onChange(null) от кнопки «✕» дропзоны идёт мимо этой ветки — обычный
      // сброс к дефолту через pending ниже (D-10).
      handleFileUpload(item, el, value);
      return;
    }
    pending.set(item.key, value);
    updateBatchBar();
  }

  // ── фото/файл: тот же staff-путь POST /app/api/uploads, что резюме делегата (без нового
  // транспорта) — полученный file_id ложится в pending обычным изменением (не сразу, как
  // тумблер — D-08 относит photo/file к «остальным типам»). 413/оффлайн/неверный тип —
  // инлайн под полем текстами реестра, дропзона остаётся кликабельной для повтора.
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
      // needs_confirm/stale на одиночном тумблере — нештатно (сервер уже подтвердил опасность
      // текстом item.confirm_text до отправки); безопасный отказ — перечитать реестр целиком,
      // не оставлять строку в неопределённом визуальном состоянии.
      await loadAndRender();
    } catch (err) {
      if (!isAuthError(err)) {
        setFieldState(el, "error", { text: errorText(err, texts.miniapp_settings_error_toast_text || "") });
      }
    } finally {
      busyToggle = false;
    }
  }

  // ── карточка группы: заголовок-кнопка (счётчик + шеврон, вращение — CSS .collapsed) +
  // тело со строками. ─────────────────────────────────────────────────────────────────────
  function buildGroupCard(group, isFirst) {
    const rowsWrap = h("div", { class: "settings-group-body" });
    for (const item of group.items) {
      const row = buildRow(item);
      itemIndex.set(item.key, row);
      setOriginal(item);
      rowsWrap.append(row.el);
    }
    const countEl = h("span", { class: "settings-group-count" });
    const chevron = h("span", { class: "settings-group-chevron" }, icon("chevron-down"));
    const wrap = h("div", { class: "settings-group" });
    wrap.dataset.token = group.token;
    const head = h("button", {
      class: "settings-group-head", type: "button", "aria-label": group.label, "aria-expanded": "true",
      onClick: () => onGroupHeadClick(group.token, wrap),
    }, h("span", { class: "settings-group-title", text: group.label }), countEl, chevron);
    wrap.append(head, rowsWrap);
    countEl.textContent = `${group.items.length}/${group.items.length}`;

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
    updateGroupCollapseVisual(wrap);
  }

  function updateGroupCollapseVisual(wrap) {
    const token = wrap.dataset.token;
    const stored = Boolean(groupCollapsedState.get(token));
    const hidden = wrap.classList.contains("search-hidden");
    const forceOpen = Boolean(query) && !hidden;
    const collapsed = forceOpen ? false : stored;
    wrap.classList.toggle("collapsed", collapsed);
    const head = wrap.querySelector(".settings-group-head");
    if (head) head.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }

  // ── раздел: eyebrow-заголовок + тумблеры раздела (флат-список строк) + карточки групп.
  // Пустой раздел не рисуется вовсе — сервер уже не присылает пустые (settings.py::_sections).
  function buildSection(section) {
    const sectionEl = h("div", { class: "settings-section" });
    sectionEl.append(sectionTitle(h, section.label));
    if (section.toggles.length) {
      const toggleList = h("div", { class: "settings-toggle-list" });
      for (const item of section.toggles) {
        const row = buildRow(item);
        itemIndex.set(item.key, row);
        setOriginal(item);
        toggleList.append(row.el);
      }
      sectionEl.append(toggleList);
    }
    section.groups.forEach((group, idx) => {
      sectionEl.append(buildGroupCard(group, idx === 0));
    });
    return sectionEl;
  }

  // ── поиск (WEB-SET-02, D-15): фильтр на каждый keystroke без debounce, подсветка через
  // highlightMatch (h("mark"), не строкой). Группа без совпадений скрывается целиком,
  // совпавшая строка временно разворачивает свою группу — сохранённая свёрнутость
  // восстанавливается при очистке (updateGroupCollapseVisual читает query по замыканию).
  function allItems() {
    return Array.from(itemIndex.values(), (r) => r.item);
  }

  function onSearchInput() {
    query = searchInput.value;
    searchClear.classList.toggle("hidden", !query);

    const results = searchFilter(allItems(), query);
    const rangesByKey = new Map(results.map((r) => [r.item.key, r.ranges]));

    let shown = 0;
    for (const [key, row] of itemIndex) {
      const matched = !query || rangesByKey.has(key);
      row.el.classList.toggle("hidden", !matched);
      if (matched) shown += 1;
      paintHighlight(row.el, row.item, query ? rangesByKey.get(key) : null);
    }

    for (const groupEl of sectionsWrap.querySelectorAll(".settings-group")) {
      const rows = groupEl.querySelectorAll(".settings-row");
      let visible = 0;
      rows.forEach((r) => { if (!r.classList.contains("hidden")) visible += 1; });
      const groupHidden = Boolean(query) && visible === 0;
      groupEl.classList.toggle("search-hidden", groupHidden);
      groupEl.classList.toggle("hidden", groupHidden);
      const countEl = groupEl.querySelector(".settings-group-count");
      if (countEl) countEl.textContent = `${visible}/${rows.length}`;
      updateGroupCollapseVisual(groupEl);
    }

    for (const sectionEl of sectionsWrap.children) {
      const rows = sectionEl.querySelectorAll(".settings-row");
      let anyVisible = false;
      rows.forEach((r) => { if (!r.classList.contains("hidden")) anyVisible = true; });
      sectionEl.classList.toggle("hidden", Boolean(query) && !anyVisible);
    }

    searchCount.textContent = (texts.miniapp_settings_search_count_text || "")
      .replace("{shown}", String(shown)).replace("{total}", String(payload ? payload.total : itemIndex.size));
    searchCount.classList.toggle("active", Boolean(query));

    const zero = Boolean(query) && shown === 0;
    sectionsWrap.classList.toggle("hidden", zero);
    renderSearchEmpty(zero);
  }

  function renderSearchEmpty(show) {
    searchEmpty.classList.toggle("hidden", !show);
    if (!show) return;
    const suggestions = suggestTerms(allItems(), query, 3);
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

  // ── загрузка/перезагрузка ────────────────────────────────────────────────────────────
  async function loadAndRender() {
    itemIndex.clear();
    groupCollapsedState.clear();
    searchInput.disabled = true;
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
    clearState();
    searchInput.disabled = false;
    searchInput.placeholder = texts.miniapp_settings_search_placeholder_text || "";

    buildCityBar(data.city_header);
    sectionsWrap.replaceChildren(...data.sections.map((s) => buildSection(s)));

    if (!data.sections.length) {
      sectionsWrap.append(emptyState(h, { me, text: texts.miniapp_settings_load_error_text || "" }));
    }

    query = searchInput.value || "";
    onSearchInput();
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
