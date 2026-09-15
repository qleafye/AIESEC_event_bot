// Анкета 2.0 (Phase 30, 30-03/30-04, A2-01..A2-06, 30-UI-SPEC.md § «По типу шага»): рендер
// НОВЫХ типов шага по контракту `reg_engine.step_spec()` — `spec.kind`/`spec.degraded_kind`/
// `spec.flags`/`spec.v2_texts` (последние два публикует план 30-03 задача 4). Диспетчер этого
// модуля переключается по `spec.degraded_kind` — уже посчитанному СЕРВЕРОМ результату
// `reg_engine.degrade_kind()` (T-30-02 threat register: деградация НЕ переизобретается на
// клиенте, обе поверхности зовут одну функцию).
//
// Правило 0-хардкода (D-25) действует и здесь: ни одной кириллической строки в коде — только
// внутри комментариев (сторож `tests/test_miniapp_form_types_js_260912.py`, план 30-03 задача
// 5). Подписи приходят из `spec.v2_texts`/`spec.label`/`spec.help`/`spec.option_labels` —
// сервер уже прогнал их через `i18n.tr()` (miniapp/routers/form.py). `innerHTML` запрещён
// (T-30-08) — узлы собираются только через `h()`/`textContent` (аргумент `h` — тот же хелпер,
// что использует form.js::buildControl, второго рендерера не заводим).
//
// Файл лежит в КОРНЕ `js/`, НЕ в `js/screens/` — это не экран, у него нет своего маршрута
// (`tests/test_miniapp_frontend.py::test_every_screen_module_is_registered_in_routes`
// требует маршрут ровно для файлов `js/screens/*.js`).
//
// Экспорт: `buildV2Control(h, spec, value, onChange, flags)` -> `{ control, footerLabel,
// disabled, onFooterChange }`. `footerLabel`/`disabled` — состояние ГЛАВНОЙ кнопки мастера НА
// МОМЕНТ вызова (первый рендер шага, `screens/form.js::drawStep`, план 30-04). Экран не
// перерисовывается на каждый `onChange` (drawStep вызывается один раз за шаг — см. докстринг
// `screens/form.js`), поэтому для типов с меняющимся состоянием кнопки (`select` до и после
// выбора, `multi` 0 vs 1+ выбрано, `link` до/после валидной ссылки) контрол дополнительно
// отдаёт `onFooterChange(cb)` — подписку, вызываемую немедленно текущим состоянием и повторно
// при каждом внутреннем изменении. Это НЕ второй источник правды: `cb` вызывается ТЕМИ ЖЕ
// вычислениями, что дали начальные `footerLabel`/`disabled`, второй копии условий в модуле нет.

import { icon } from "./icons.js";
import { labelText } from "./ui.js";
// `api.js` — ДИНАМИЧЕСКИЙ импорт внутри `lookupControl` (ниже), не статический здесь: его
// модульный верх читает `window.Telegram` немедленно при импорте (D-12), а node-подпроцессы
// JS-сторожей (`tests/test_miniapp_form_controls_js_260911.py` и соседи) стабят только
// `globalThis.document`, не `window` — статический импорт уронил бы КАЖДЫЙ тест `form.js`
// (он импортирует этот модуль), даже те, что не трогают `lookup` вовсе. Экраны `screens/*.js`
// той же причине получают `api` параметром `ctx`, а не импортом (`screens/form.js::render`).

/**
 * Локальный хелпер вибрации (30-UI-SPEC.md § Motion) — единственное место в этом модуле, где
 * читается `tg.HapticFeedback`. Молчит, если флаг `haptics` выключен — управляет ТОЛЬКО
 * модулем анкеты, не всем Mini App (общий мотор мотора уровней движения — `motion.js`,
 * второй копии детекта уровня здесь не заводим, хаптика анкеты — отдельная ручка).
 */
function haptic(kind, flags) {
  if (!flags || !flags.haptics) return;
  const tg = window.Telegram && window.Telegram.WebApp;
  const feedback = tg && tg.HapticFeedback;
  if (!feedback) return;
  try {
    if (kind === "light" && typeof feedback.impactOccurred === "function") {
      feedback.impactOccurred("light");
    } else if (typeof feedback.notificationOccurred === "function") {
      feedback.notificationOccurred(kind);
    }
  } catch (_) { /* конкретный клиент без метода — не критично */ }
}

// ── select: плитки с пояснением (30-UI-SPEC.md § «1. select») ──────────────────────────────

function selectTiles(h, spec, value, onChange, flags) {
  const texts = spec.v2_texts || {};
  const hints = spec.option_hints || {};
  const box = h("div", { class: "opt-tiles", role: "radiogroup", "aria-label": spec.label });
  const tiles = [];
  let current = value;
  let footerCb = null;

  function footerState() {
    const picked = current != null && current !== "";
    return { label: picked ? null : (texts.pick_option || null), disabled: !picked };
  }

  function notify() {
    if (!footerCb) return;
    const s = footerState();
    footerCb(s.label, s.disabled);
  }

  function paint() {
    for (const t of tiles) {
      const on = t.opt === current;
      t.el.classList.toggle("on", on);
      t.el.setAttribute("aria-checked", on ? "true" : "false");
    }
    notify();
  }

  for (const opt of spec.options || []) {
    const label = (spec.option_labels && spec.option_labels[opt]) || opt;
    const hint = hints[opt] || "";
    const tile = h("button", {
      class: "opt-tile", type: "button", role: "radio", "aria-checked": "false",
      onClick: () => {
        current = opt;
        onChange(opt);
        paint();
        haptic("light", flags);
      },
    },
      h("span", { class: "tt", text: label }),
      hint ? h("span", { class: "th", text: hint }) : null,
      h("span", { class: "mark" }, icon("check")),
    );
    tiles.push({ opt, el: tile });
    box.append(tile);
  }
  paint();

  const note = texts.selection_visible_note
    ? h("p", { class: "label-role", text: texts.selection_visible_note })
    : null;

  const initial = footerState();
  return {
    control: h("div", {}, box, note),
    footerLabel: initial.label,
    disabled: initial.disabled,
    onFooterChange: (cb) => { footerCb = cb; notify(); },
  };
}

// ── multi: чипы с лимитом и счётчиком (30-UI-SPEC.md § «5. multi») ─────────────────────────

function multiChips(h, spec, value, onChange, flags) {
  const texts = spec.v2_texts || {};
  const maxSelect = typeof spec.max_select === "number" ? spec.max_select : null;
  const chosen = new Set(Array.isArray(value) ? value : []);
  const chips = [];
  let footerCb = null;

  const showCounter = !!(flags && flags.limit_counter) && maxSelect != null;
  const counter = showCounter ? h("span", { class: "cnt" }) : null;
  const heading = counter
    ? h("p", { class: "label-role" }, counter)
    : null;
  const chipsBox = h("div", { class: "chips", "data-mode": "multi", role: "group", "aria-label": spec.label });
  const hint = h("p", { class: "label-role", "aria-live": "polite" });
  const otherInput = h("input", { class: "input", type: "text", placeholder: texts.own_option || "" });
  const otherRow = h("div", { class: "pick" }, icon("plus"), h("div", { class: "cv" }, otherInput));

  function limitHintText() {
    if (maxSelect == null) return "";
    const n = chosen.size;
    if (n === 0) {
      return String(texts.limit_hint_zero || "").replace("{max}", String(maxSelect));
    }
    const tpl = n >= maxSelect ? texts.limit_hint_max : texts.limit_hint_mid;
    return String(tpl || "")
      .replace("{n}", String(n))
      .replace("{max}", String(maxSelect))
      .replace("{left}", String(Math.max(0, maxSelect - n)));
  }

  function footerState() {
    return {
      label: chosen.size > 0 ? (texts.continue_button || null) : (texts.skip_button || null),
      disabled: false,
    };
  }

  function notify() {
    if (!footerCb) return;
    const s = footerState();
    footerCb(s.label, s.disabled);
  }

  function paint() {
    for (const c of chips) {
      c.el.classList.toggle("on", chosen.has(c.opt));
    }
    if (counter) counter.textContent = `${chosen.size}/${maxSelect}`;
    hint.textContent = limitHintText();
    notify();
  }

  function shake(el) {
    if (el && typeof el.animate === "function") {
      el.animate(
        [
          { transform: "translateX(0)" },
          { transform: "translateX(-3px)" },
          { transform: "translateX(3px)" },
          { transform: "translateX(0)" },
        ],
        { duration: 180 },
      );
    }
  }

  function addValue(opt) {
    if (!opt || chosen.has(opt)) return;
    if (maxSelect != null && chosen.size >= maxSelect) return false;
    chosen.add(opt);
    return true;
  }

  function toggle(opt, el) {
    if (chosen.has(opt)) {
      chosen.delete(opt);
    } else if (maxSelect != null && chosen.size >= maxSelect) {
      shake(el);
      haptic("warning", flags);
      return;
    } else {
      chosen.add(opt);
    }
    onChange(Array.from(chosen));
    paint();
    haptic("light", flags);
  }

  for (const opt of spec.options || []) {
    const label = (spec.option_labels && spec.option_labels[opt]) || opt;
    const chip = h("button", {
      class: "chip-pick", type: "button", "aria-pressed": "false",
      onClick: (ev) => toggle(opt, ev && ev.currentTarget),
    }, h("span", { text: label }));
    chips.push({ opt, el: chip });
    chipsBox.append(chip);
  }

  function commitOwnValue() {
    const raw = otherInput.value.trim();
    if (!raw) return;
    if (addValue(raw) === false) {
      shake(otherRow);
      haptic("warning", flags);
      return;
    }
    otherInput.value = "";
    onChange(Array.from(chosen));
    paint();
  }
  otherInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commitOwnValue(); }
  });
  otherInput.addEventListener("blur", commitOwnValue);

  paint();

  const initial = footerState();
  return {
    control: h("div", {}, heading, chipsBox, otherRow, hint),
    footerLabel: initial.label,
    disabled: initial.disabled,
    onFooterChange: (cb) => { footerCb = cb; notify(); },
  };
}

// ── link: карточка ссылки с распознаванием формата (30-UI-SPEC.md § «4. link») ──────────────

function isHttpUrl(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch (_) {
    return false;
  }
}

function linkCard(h, spec, value, onChange, flags) {
  const texts = spec.v2_texts || {};
  // Не каждый `link`-шаг — URL: ВК хранит `@юзернейм` (`spec.type` остаётся `"text"`,
  // `step_type_v2` override — только новая ось), резюме-ссылка — настоящий `url`
  // (`_UI_TYPE_OVERRIDES["resume_link"] = "url"`). Клавиатура/валидация формата дальше
  // зависят от РЕАЛЬНОГО формата поля, не от факта попадания в тип `link`.
  const isUrlShaped = spec.type === "url";
  const fieldWrap = h("div", { class: "field" });
  const input = h("input", {
    class: "input", id: `f-${spec.key}`,
    type: isUrlShaped ? "url" : "text",
    inputmode: isUrlShaped ? "url" : undefined,
  });
  input.value = value || "";
  const row = h("div", { class: "pick" },
    h("span", { class: "svc-icon" }, icon(spec.link_icon || "link")),
    h("div", { class: "cv" }, input),
  );
  fieldWrap.append(row);
  const hint = spec.help ? h("p", { class: "label-role", text: spec.help }) : null;
  const errorLine = h("p", { class: "field-error hidden" });
  const okline = h("p", { class: "okline hidden" }, icon("check"), h("span", { text: texts.recognized || "" }));
  const card = h("div", { class: "card" }, fieldWrap, hint, errorLine, okline);
  let footerCb = null;

  function footerState() {
    const raw = input.value.trim();
    const blocked = !!spec.required && !raw;
    return { label: null, disabled: blocked };
  }

  function notify() {
    if (!footerCb) return;
    const s = footerState();
    footerCb(s.label, s.disabled);
  }

  function paint() {
    const raw = input.value.trim();
    fieldWrap.classList.remove("ok", "bad");
    okline.classList.add("hidden");
    errorLine.classList.add("hidden");
    if (raw) {
      // Формат проверяем ТОЛЬКО у настоящего URL (резюме-ссылка) — у ВК-подобного
      // юзернейма нет клиентского регэкспа в этом модуле (правило `validate_answer`
      // сервера не дублируем второй копией, T-21-05); непустое значение сразу — «ok».
      const recognized = isUrlShaped ? isHttpUrl(raw) : true;
      if (recognized) {
        fieldWrap.classList.add("ok");
        okline.classList.remove("hidden");
      } else {
        fieldWrap.classList.add("bad");
        errorLine.textContent = spec.invalid_hint_text || "";
        errorLine.classList.remove("hidden");
      }
    }
    notify();
  }

  input.addEventListener("input", () => { onChange(input.value); paint(); haptic("light", flags); });
  input.addEventListener("blur", paint);
  paint();

  const initial = footerState();
  return {
    control: card,
    footerLabel: initial.label,
    disabled: initial.disabled,
    onFooterChange: (cb) => { footerCb = cb; notify(); },
  };
}

// ── lookup: поиск + чипы + свой вариант (30-UI-SPEC.md § «2. lookup») ──────────────────────
// Порядок узлов дословно из спеки: поиск → топ-8 чипов → ghost-чип «Другой {entity}» →
// построчный список результатов → пустое состояние. Деградация — СТРОГО из `spec.lookup`
// (30-08 задача A: `reg_engine.lookup_render_flags` уже сводит глобальные тумблеры `chips`/
// `lookup_search` С атрибутами конкретного списка — глобальный `off` → `off`, глобальный
// `on` → решает атрибут списка; компонент читает готовый результат, второй раз правило не
// пересчитывает). `chips_enabled=false` прячет ряд чипов, `search_enabled=false` прячет поле
// поиска (чипы остаются статичным списком кнопок — тот же узел `renderChips`, второй разметки
// не заводим). Оба `false` одновременно (менеджер выключил ОБА атрибута списка при включённых
// глобальных тумблерах — `degrade_kind()` этого не видит, он знает только про глобальные)
// компонент разворачивает сам, в голое текстовое поле — тот же приём, что уже применяет
// `degrade_kind()` на глобальном уровне (30-UI-SPEC.md §2 «Оба выключены»).

const LOOKUP_MIN_QUERY = 2;
const LOOKUP_DEBOUNCE_MS = 250;

function lookupIconFor(spec) {
  return spec.key === "university" ? "graduation-cap" : "map-pin";
}

function lookupControl(h, spec, value, onChange, flags) {
  const lk = spec.lookup || {};
  const showChips = !!lk.chips_enabled;
  const showSearch = !!lk.search_enabled;
  if (!showChips && !showSearch) {
    return textField(h, spec, value, onChange);
  }
  const texts = spec.v2_texts || {};

  let otherAllowed = false;
  let debounceId = null;

  const searchInput = h("input", { class: "input", type: "text" });
  searchInput.value = value || "";
  const searchRow = h("div", { class: "pick" }, icon("search"), h("div", { class: "cv" }, searchInput));
  const chipsBox = h("div", { class: "chips" });
  const ghostChip = h("button", { class: "chip-pick ghost hidden", type: "button" },
    h("span", { text: texts.own_chip || "" }));
  const list = h("div", { class: "list" });
  const emptyState = h("div", { class: "hidden" });
  const ownInput = h("input", { class: "input hidden", type: "text" });

  function selectValue(canonical) {
    onChange(canonical);
    searchInput.value = canonical;
    list.replaceChildren();
    emptyState.classList.add("hidden");
  }

  function openOwn(initialText) {
    if (!otherAllowed) return;
    ownInput.classList.remove("hidden");
    ownInput.value = initialText || "";
    ownInput.focus();
  }

  ownInput.addEventListener("input", () => onChange(ownInput.value));
  ghostChip.addEventListener("click", () => openOwn(searchInput.value));

  function renderChips(chips) {
    chipsBox.replaceChildren();
    if (!showChips) return;
    for (const label of chips) {
      const chip = h("button", { class: "chip-pick", type: "button" }, h("span", { text: label }));
      chip.addEventListener("click", () => selectValue(label));
      chipsBox.append(chip);
    }
    ghostChip.classList.toggle("hidden", !otherAllowed);
    if (otherAllowed) chipsBox.append(ghostChip);
  }

  function renderResults(results, query) {
    list.replaceChildren();
    if (results.length) {
      emptyState.classList.add("hidden");
      for (const r of results) {
        const row = h("button", { class: "row", type: "button" }, icon(lookupIconFor(spec)), h("span", { text: r.canonical }));
        row.addEventListener("click", () => selectValue(r.canonical));
        list.append(row);
      }
      return;
    }
    if (!query) { emptyState.classList.add("hidden"); return; }
    const title = String(texts.empty_title || "").replace("{query}", query);
    // «Свой вариант» выключен (`other_allowed=false`, атрибут списка — план 30-07): пустое
    // состояние показывает только заголовок запроса, БЕЗ приглашения вписать своё — второй
    // строки-приглашения без включённого атрибута тоже нет. Отдельный текст «такого нет в
    // списке» (30-UI-SPEC.md §2) заводит план 30-07 вместе с самим атрибутом — сегодня
    // `other_allowed` уже приходит `false` по умолчанию почти для всех lookup-шагов
    // (`_OTHER_ALLOWED_STEPS` не включает `university`/`city` целиком), плодить неточный
    // текст без ключа реестра раньше срока не станем (правило 0-хардкода).
    emptyState.replaceChildren(h("p", { class: "label-role", text: title }));
    if (otherAllowed) {
      emptyState.append(h("p", { class: "label-role", text: texts.own_option || "" }));
    }
    emptyState.classList.remove("hidden");
  }

  async function fetchSuggest(q) {
    let res;
    try {
      const { api } = await import("./api.js");
      res = await api(`/reg/suggest?step=${encodeURIComponent(spec.key)}&q=${encodeURIComponent(q || "")}`);
    } catch (_) {
      return;
    }
    otherAllowed = !!res.other_allowed;
    renderChips(res.chips || []);
    renderResults(res.results || [], q);
  }

  if (showSearch) {
    searchInput.addEventListener("input", () => {
      onChange(searchInput.value);
      if (debounceId) clearTimeout(debounceId);
      const q = searchInput.value.trim();
      if (q.length < LOOKUP_MIN_QUERY) { list.replaceChildren(); emptyState.classList.add("hidden"); return; }
      debounceId = setTimeout(() => fetchSuggest(q), LOOKUP_DEBOUNCE_MS);
    });
  }

  fetchSuggest("");   // первичная загрузка чипов/other_allowed — не ждёт ввода делегата

  const hint = texts.hint_default ? h("p", { class: "label-role", text: texts.hint_default }) : null;
  const children = [];
  if (showSearch) { children.push(searchRow, hint); }
  children.push(chipsBox, list, emptyState, ownInput);

  return { control: h("div", {}, ...children), footerLabel: null, disabled: false };
}

// ── text/phone: рестайл поля мастера (30-UI-SPEC.md § «7. text») ───────────────────────────
// Кнопка «Поделиться номером» НЕ рисуется здесь — это отдельный, уже существующий узел
// (`screens/form.js::shareContactButton`/`canShareContact`, реализован планом 21-11, план
// 30-03 задача 4 только меняет иконку/подпись) — второй реализации `requestContact` не
// заводим (30-PATTERNS.md § «screens/form.js — drawStep() extension»).

function textField(h, spec, value, onChange) {
  const isPhone = spec.type === "phone";
  const input = h("input", { class: "input", id: `f-${spec.key}`, type: isPhone ? "tel" : "text" });
  if (isPhone) input.setAttribute("inputmode", "tel");
  if (spec.max_len) input.setAttribute("maxlength", String(spec.max_len));
  input.value = value || "";
  input.addEventListener("input", () => onChange(input.value));
  return { control: input, footerLabel: null, disabled: false };
}

// ── composite: карточка «Образование» (30-UI-SPEC.md § «3. composite», план 30-04) ─────────
// Карточка собирается ТОЛЬКО из частей, которые реально прислал сервер (`spec.composite.parts`
// — `reg_engine.composite_parts()` уже отфильтровала по событию, 30-CONTEXT.md реш. 2): одна
// включённая часть — карточка из одной строки, а не откат к легаси-шагу. Рендер ищет части ПО
// `key`, не хардкодит «у события ровно четыре части».
//
// ВУЗ переиспользует ОБЩИЙ диспетчер (`buildV2Control` рекурсивно) — у него собственный
// `degraded_kind` (`"lookup"`, независимо от группы, T-30-04-02 в reg_engine.py), второго
// поиска не заводим. Курс/программа — часть группы (их `degraded_kind` тоже `"composite"`,
// рекурсивный вызов `buildV2Control` на НИХ дал бы бесконечный рендер этой же карточки внутри
// себя) — рисуются точечно: курс чипами из `part.options`, программа — обычным полем.
//
// `onChange` карточки отдаёт наверх ОБЪЕКТ `{step_key: value, ...}` (патч НЕСКОЛЬКИХ колонок
// одним шагом), а не скаляр, как у остальных типов — composite ЕДИНСТВЕННЫЙ тип, отвечающий
// сразу за четыре колонки одной карточкой. `screens/form.js::goNext()` (план 30-05, задача 0б)
// узнаёт объектный патч по `typeof === "object"` и шлёт все его колонки одним PATCH.
//
// `spec.composite.parts[*].value`/`.prior` и `spec.composite.studying` подмешивает
// `reg_engine.form_spec()` (план 30-05, задача 0б) — карточка стартует с прежними ответами
// делегата, а не всегда пустая (30-04-SUMMARY.md, «Known Stubs»).

function compositeCard(h, spec, value, onChange, flags) {
  const texts = spec.v2_texts || {};
  const comp = spec.composite || { parts: [], toggle_step: null };
  const partByKey = {};
  for (const part of comp.parts) partByKey[part.key] = part;
  const toggleKey = comp.toggle_step;
  const togglePart = toggleKey ? partByKey[toggleKey] : null;
  // Task 260915-skg (P1b, решение владельца «две плитки»): сервер отдаёт готовое разбиение
  // вариантов `education_status` — карточка не держит свой список «учащихся» статусов
  // (T-skg-03, D-06 фазы 28 — единственная точка правды `is_studying`/`edu_studying_statuses`).
  const studyingOption = comp.studying_option || null;
  const notStudying = comp.not_studying_options || [];
  // Визуальный порядок карточки — фиксированный (ВУЗ, курс, программа), НЕ порядок REG_FLOW
  // (`composite_parts()` отдаёт `education_status` первым) — рисуются только части из ответа.
  const fieldKeys = ["university", "course", "study_field"].filter(
    (key) => key !== toggleKey && partByKey[key],
  );

  const state = {};
  for (const key of fieldKeys) state[key] = (partByKey[key] && partByKey[key].value) || "";
  // Дефолт тумблера — «учится» (тот же дефолт, что у первого варианта самого вопроса), но
  // `spec.composite.studying` (план 30-05, задача 0б) побеждает дефолт, когда сервер уже знает
  // прежний ответ делегата (`reg_engine.form_spec` вычисляет его через `is_studying`, единая
  // точка правды — клиент не дублирует список «учащихся» статусов реестра).
  let studying = comp.studying !== false;

  // Task 260915-skg (P1b): `state[toggleKey]` теперь СТРОКА education_status, никогда bool.
  // ВКЛ -> `studyingOption` с сервера; ВЫКЛ -> прежний ответ делегата, если он входит в
  // `notStudying` (предвыбор плитки при повторном заходе), иначе пусто (ничего не выбрано).
  if (toggleKey) {
    state[toggleKey] = studying
      ? studyingOption
      : (togglePart && notStudying.includes(togglePart.value) ? togglePart.value : "");
  }

  // Per-part «есть ошибка» — булев флаг, НЕ текст: клиентский пред-показ лимита длины (ниже, у
  // программы обучения) не дублирует текст ошибки сервера (`reg_engine.validate_answer`,
  // T-21-05 «второго валидатора нет») — только красная рамка `.field.is-error` (уже
  // существующий класс, `app.css`), сам текст покажет ответ сервера, когда многоколоночный
  // commit карточки довяжут (см. «Known Stubs» в SUMMARY плана 30-04). Ошибка ОДНОЙ части
  // блокирует ТОЛЬКО главную кнопку (через `onFooterChange`), не рендер/интерактивность соседних.
  const errorFlags = {};
  // Task 260915-skg (P1b): начальная ошибка тумблера — «выкл и ничего не выбрано» ИЛИ «вкл, но
  // реестр без учащихся вариантов» — оба случая сводятся к «state[toggleKey] пуст».
  if (toggleKey) errorFlags[toggleKey] = !state[toggleKey];
  let footerCb = null;

  function emit() {
    onChange({ ...state });
  }

  function footerState() {
    const hasError = Object.values(errorFlags).some(Boolean);
    return { label: null, disabled: hasError };
  }
  function notifyFooter() {
    if (!footerCb) return;
    const s = footerState();
    footerCb(s.label, s.disabled);
  }
  function setError(key, hasError) {
    errorFlags[key] = !!hasError;
    notifyFooter();
  }

  function optionalBadge(part) {
    return part.required === false
      ? h("span", { class: "opt", text: texts.optional_badge_text || "" })
      : null;
  }

  function buildUniversityPart(part) {
    const sub = buildV2Control(h, part, state[part.key], (v) => {
      state[part.key] = v;
      emit();
    }, flags);
    return h("div", { class: "cpart" },
      h("div", { class: "cl" }, h("span", { text: labelText(part.label) })),
      sub.control,
    );
  }

  function buildChipsPart(part) {
    const box = h("div", { class: "chips" });
    const chipEls = [];
    for (const opt of part.options || []) {
      const optLabel = (part.option_labels && part.option_labels[opt]) || opt;
      const chip = h("button", { class: "chip-pick", type: "button" }, h("span", { text: optLabel }));
      chip.addEventListener("click", () => {
        state[part.key] = opt;
        for (const c of chipEls) c.el.classList.toggle("on", c.opt === opt);
        emit();
        haptic("light", flags);
      });
      chipEls.push({ opt, el: chip });
      box.append(chip);
    }
    for (const c of chipEls) c.el.classList.toggle("on", c.opt === state[part.key]);
    return h("div", { class: "cpart" },
      h("div", { class: "cl" }, h("span", { text: labelText(part.label) })),
      box,
    );
  }

  function buildTextPart(part) {
    const fieldWrap = h("div", { class: "field" });
    const input = h("input", { class: "input", type: "text" });
    input.value = state[part.key] || "";
    if (part.max_len) input.setAttribute("maxlength", String(part.max_len));
    fieldWrap.append(input);
    input.addEventListener("input", () => {
      state[part.key] = input.value;
      const overLimit = Boolean(part.max_len) && input.value.length > part.max_len;
      fieldWrap.classList.toggle("is-error", overLimit);
      setError(part.key, overLimit);
      emit();
    });
    return h("div", { class: "cpart" },
      h("div", { class: "cl" }, h("span", { text: labelText(part.label) }), optionalBadge(part)),
      fieldWrap,
    );
  }

  function buildPart(key) {
    const part = partByKey[key];
    if (!part) return null;
    if (key === "university") return buildUniversityPart(part);
    if (Array.isArray(part.options) && part.options.length) return buildChipsPart(part);
    return buildTextPart(part);
  }

  const fullParts = fieldKeys.map(buildPart).filter(Boolean);
  const doneCpart = h("div", { class: "cpart hidden" },
    h("div", { class: "cl", text: texts.subtitle || "" }),
    h("div", { class: "cv", text: texts.done_hint || "" }),
  );

  // Task 260915-skg (P1b): плитки статуса «не учащегося» делегата — тот же приём, что
  // `buildChipsPart`, но источник подписей — `notStudying` с сервера (голые строки, не
  // `part.options`/`part.option_labels`).
  const statusChipEls = [];
  const statusChipsBox = h("div", { class: "chips" });
  for (const opt of notStudying) {
    const chip = h("button", { class: "chip-pick", type: "button" }, h("span", { text: opt }));
    chip.addEventListener("click", () => {
      state[toggleKey] = opt;
      for (const c of statusChipEls) c.el.classList.toggle("on", c.opt === opt);
      setError(toggleKey, false);
      emit();
      haptic("light", flags);
    });
    statusChipEls.push({ opt, el: chip });
    statusChipsBox.append(chip);
  }
  for (const c of statusChipEls) c.el.classList.toggle("on", c.opt === state[toggleKey]);
  const statusChipsCpart = toggleKey ? h("div", { class: "cpart" }, statusChipsBox) : null;

  function paintToggleVisibility() {
    for (const node of fullParts) {
      node.classList.toggle("hidden", !studying);
      node.setAttribute("aria-hidden", studying ? "false" : "true");
    }
    doneCpart.classList.toggle("hidden", studying);
    if (statusChipsCpart) {
      statusChipsCpart.classList.toggle("hidden", studying);
      statusChipsCpart.setAttribute("aria-hidden", studying ? "true" : "false");
    }
  }

  const toggleTitle = h("div", { class: "st" });
  const toggleHint = h("div", { class: "ss" });
  const togglePill = h("span", { class: "sw" }, h("i", {}));
  function paintToggleLabels() {
    toggleTitle.textContent = studying ? (texts.toggle_on_label || "") : (texts.toggle_off_label || "");
    toggleHint.textContent = studying ? (texts.toggle_on_hint || "") : (texts.toggle_off_hint || "");
    togglePill.classList.toggle("on", studying);
  }
  const toggleRow = h("div", { class: "swrow", role: "switch", "aria-checked": "true" },
    h("div", {}, toggleTitle, toggleHint), togglePill,
  );
  toggleRow.addEventListener("click", () => {
    studying = !studying;
    toggleRow.setAttribute("aria-checked", studying ? "true" : "false");
    paintToggleLabels();
    paintToggleVisibility();
    // Task 260915-skg (P1b): ВКЛ -> studyingOption; ВЫКЛ -> оставить уже выбранный чип (если он
    // валиден среди notStudying), иначе пусто. Никогда true/false в state.
    if (toggleKey) {
      if (studying) {
        state[toggleKey] = studyingOption;
      } else if (!notStudying.includes(state[toggleKey])) {
        state[toggleKey] = "";
      }
      for (const c of statusChipEls) c.el.classList.toggle("on", c.opt === state[toggleKey]);
      setError(toggleKey, !state[toggleKey]);
    }
    emit();
    haptic("light", flags);
  });
  paintToggleLabels();
  paintToggleVisibility();

  const card = h("div", { class: "card brand" }, ...fullParts, doneCpart, statusChipsCpart,
    togglePart ? h("div", { class: "cpart" }, toggleRow) : null);
  const note = texts.toggle_note ? h("p", { class: "label-role", text: texts.toggle_note }) : null;

  return {
    control: h("div", {}, card, note),
    footerLabel: null,
    disabled: footerState().disabled,
    onFooterChange: (cb) => { footerCb = cb; notifyFooter(); },
  };
}

// ── repeatable: несколько блоков «название + описание» (30-UI-SPEC.md § «6. repeatable») ───
// Хранение — список объектов `{title, description}` (30-CONTEXT.md, решение владельца 12.09);
// сериализацию в JSON делает СЕРВЕР (`reg_engine.dump_repeatable`, `validate_answer`) — здесь
// второй копии формата нет, `onChange` отдаёт наружу голый массив объектов как есть.

function repeatableBlockView(h, item, index, texts, onEditClick) {
  const editAction = h("span", { class: "opt", text: texts.edit_action || "" });
  editAction.addEventListener("click", onEditClick);
  const label = String(texts.item_label || "").replace("{n}", String(index + 1));
  return h("div", { class: "card" },
    h("div", { class: "cpart" },
      h("div", { class: "cl" }, h("span", { text: label }), editAction),
      h("div", { class: "rep-title", text: item.title || "" }),
      item.description ? h("div", { class: "rep-desc", text: item.description }) : null,
    ),
  );
}

function repeatableBlockEdit(h, item, index, texts, onDone) {
  const label = String(texts.item_label || "").replace("{n}", String(index + 1));
  const titleInput = h("input", { class: "input", type: "text", placeholder: label });
  titleInput.value = item.title || "";
  const descInput = h("textarea", { class: "input" });
  descInput.value = item.description || "";
  titleInput.addEventListener("input", () => onDone({ title: titleInput.value, description: item.description }, true));
  descInput.addEventListener("input", () => onDone({ title: item.title, description: descInput.value }, true));
  const doneBtn = h("button", { class: "chip-pick", type: "button" }, h("span", { text: texts.edit_action || "" }));
  doneBtn.addEventListener("click", () => onDone({ title: titleInput.value, description: descInput.value }, false));
  return h("div", { class: "card" },
    h("div", { class: "cpart" },
      h("div", { class: "cl", text: label }),
      h("div", { class: "field" }, titleInput),
      h("div", { class: "field" }, descInput),
      doneBtn,
    ),
  );
}

function repeatableList(h, spec, value, onChange, flags) {
  const texts = spec.v2_texts || {};
  const maxItems = typeof spec.repeatable_max === "number" ? spec.repeatable_max : null;
  const items = (Array.isArray(value) ? value : []).map((it) => ({
    title: (it && it.title) || "", description: (it && it.description) || "",
  }));
  const editing = new Set(items.length ? [] : []);
  const list = h("div", {});
  const addBtn = h("button", { class: "dash", type: "button" },
    icon("plus"), h("span", { text: texts.add_button || "" }));
  const note = texts.chat_parity_note ? h("p", { class: "label-role", text: texts.chat_parity_note }) : null;

  function emit() {
    onChange(items.map((it) => ({ title: it.title, description: it.description })));
  }

  function paintAddButton() {
    addBtn.classList.toggle("hidden", maxItems != null && items.length >= maxItems);
  }

  function paint() {
    list.replaceChildren(...items.map((item, index) => (
      editing.has(index)
        ? repeatableBlockEdit(h, item, index, texts, (patch, keepEditing) => {
          items[index] = patch;
          emit();
          if (!keepEditing) { editing.delete(index); paint(); }
        })
        : repeatableBlockView(h, item, index, texts, () => { editing.add(index); paint(); })
    )));
    paintAddButton();
  }

  addBtn.addEventListener("click", () => {
    if (maxItems != null && items.length >= maxItems) return;
    items.push({ title: "", description: "" });
    editing.add(items.length - 1);
    emit();
    paint();
    haptic("light", flags);
  });

  paint();
  return { control: h("div", {}, list, addBtn, note), footerLabel: null, disabled: false };
}

// ── диспетчер ────────────────────────────────────────────────────────────────────────────

/**
 * @param {(tag: string, attrs?: object, ...children: any[]) => HTMLElement} h
 * @param {object} spec - `reg_engine.step_spec()`, обязаны быть заполнены `spec.kind`/
 *   `spec.degraded_kind` (план 30-03 задача 4)
 * @param {*} value
 * @param {(v: *) => void} onChange
 * @param {object} flags - `reg_engine.form_v2_flags()`, девять булевых без префикса
 * @returns {{control: HTMLElement, footerLabel: (string|null), disabled?: boolean,
 *   onFooterChange?: (cb: (label: (string|null), disabled: boolean) => void) => void}}
 */
export function buildV2Control(h, spec, value, onChange, flags) {
  switch (spec.degraded_kind) {
    case "select":
      return selectTiles(h, spec, value, onChange, flags);
    case "multi":
      return multiChips(h, spec, value, onChange, flags);
    case "link":
      return linkCard(h, spec, value, onChange, flags);
    case "lookup":
      return lookupControl(h, spec, value, onChange, flags);
    case "text":
      return textField(h, spec, value, onChange);
    case "composite":
      return compositeCard(h, spec, value, onChange, flags);
    case "repeatable":
      return repeatableList(h, spec, value, onChange, flags);
    default:
      // Незнакомый/ещё не реализованный kind — безопасный откат к голому текстовому полю, а
      // не исключение: делегат всё ещё может ответить, менеджер увидит проблему в логах, а
      // не в разбитом шаге.
      return textField(h, { ...spec, type: "text" }, value, onChange);
  }
}
