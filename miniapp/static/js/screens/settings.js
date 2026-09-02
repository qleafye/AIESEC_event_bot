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

import { flatRow, sectionTitle, emptyState, errorState } from "../ui.js";
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

// Маркер состояния строки (UI-SPEC §Color/Typography): per-city直 приоритетнее default/set/
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

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;

  // ── состояние экрана ────────────────────────────────────────────────────────────────
  let texts = {};
  let payload = null;
  const itemIndex = new Map(); // key -> { item, el }
  const groupCollapsedState = new Map(); // token -> bool
  let query = "";

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

  function onFieldChange(item, el, value) {
    // Задача 1 — только каркас: значение фиксируется локально на самом контроле, сохранением
    // занимается задача 2 (гибридный batch/toggle). Тумблер тут ничего не пишет — иначе
    // отрисовал бы "включено" до реального ответа сервера (D-08 требует именно так).
  }

  // ── карточка группы: заголовок-кнопка (счётчик + шеврон, вращение — CSS .collapsed) +
  // тело со строками. ─────────────────────────────────────────────────────────────────────
  function buildGroupCard(group, isFirst) {
    const rowsWrap = h("div", { class: "settings-group-body" });
    for (const item of group.items) {
      const row = buildRow(item);
      itemIndex.set(item.key, row);
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
}
