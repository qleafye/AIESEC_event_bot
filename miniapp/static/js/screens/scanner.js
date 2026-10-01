// Экран «Сканер» (Phase 12/форум-ночь п.5, FORUM-CHECKIN.md, D-08/D-11/D-12/D-13/D-18..D-20,
// идея №9): отметка на форуме. Основной путь — Telegram.WebApp.showScanQrPopup. Колбэк
// qrTextReceived САМ по себе синхронный (Telegram зовёт его сразу после чтения QR, до любого
// ответа сервера) — решение «закрыть/оставить попап» по факту (🟢 new/moved / 🟡 duplicate /
// 🔴 отказ) известно только ПОСЛЕ асинхронного /checkin/scan. Поэтому колбэк всегда возвращает
// false (попап не закрывается сам), а закрытие для не-🟢-исходов делает submitScan() явным
// вызовом `tg.closeScanQrPopup()`, когда ответ уже пришёл (квик A3): 🟢 new/moved — попап
// остаётся открытым, вибрация success, следующий скан продолжает отмечать без повторного тапа
// «Сканировать» (D-08); 🟡 duplicate и 🔴 любой отказ/не найден/чужое мероприятие/другой город
// форума/ошибка сети — попап закрывается, плашка с причиной получает кнопку «Сканировать
// дальше» (повторно открывает попап). Запасной путь на этом же экране — поиск по фамилии
// (D-11/D-12: телефон делегата сел, а сеть есть), кнопка «Отметить» шлёт ту же отметку через
// /checkin/manual (свой попап не открывает — закрывать нечего). Счётчик прихода вверху —
// /checkin/stats (задача A2: построчно по городам, когда сервер отдаёт `cities`, иначе один
// общий счётчик).
//
// Точки (D-18): «Вход» + сессии СЕГОДНЯ (/checkin/points) — «идёт сейчас» первыми, каждая с
// собственным счётчиком отмеченных (+ вместимость зала, если задана). Менеджер без
// закреплённого города видит селектор города (сервер отдаёт `cities`) — точки сессий грузятся
// заново при выборе.
//
// Защита от повторного скана: камера в непрерывном режиме присылает ОДИН И ТОТ ЖЕ текст QR
// много раз за секунды, пока волонтёр не отвёл камеру — `scan_gate.js` глушит повторы того же
// текста, а QR следующего делегата, пойманный во время отправки, ставит в очередь (не
// выбрасывает). Занятость снимается сразу по ответу на скан; счётчики грузятся без ожидания.
//
// Регистрация на месте (D-41, 27.09) — только при включённом тумблере города стойки
// (`onsite_enabled` из /checkin/points, D-36): на 🔴 «не одобрен / прошлый сезон» — кнопка
// «Пропустить и одобрить» (всегда через подтверждение с именем, один человек за запрос), на
// отклонённой менеджером — только «Пропустить вопреки отказу» со своим подтверждением, на
// «не найден» и пустом поиске — QR короткой анкеты (data URI в JSON: тег img не шлёт initData),
// список «Ждут на стойке» — walk-in сегодняшнего дня своего города. Подписи — /checkin/net-texts.

import { atUsername, flatRow, errorText, noticeBox } from "../ui.js";
import { haptic } from "../motion.js";
import { createNetHealth, timed } from "../net_health.js";
import { createScanGate, HOLD } from "../scan_gate.js";

const ENTRY_POINT = "entry";
const TRAINING_POINT = "training"; // «🧪 Тренировка» ничего не пишет — кнопок «на месте» там нет
const SEARCH_DEBOUNCE_MS = 300;
const COUNTERS_DEBOUNCE_MS = 800;

const NETWORK_TEXT = "Нет связи — переходите на приложение-сканер.";
// Скан и отметка ждут ответа не дольше SCAN_TIMEOUT_MS: на «подвисшем» Wi-Fi площадки fetch
// висит минутами, а сканер всё это время не принимал бы новые QR.
const SCAN_TIMEOUT_MS = 7000;
const TIMEOUT_TEXT = "Нет ответа от сервера — отсканируйте ещё раз (если отметка всё же прошла, покажет «Уже был»). Повторяется — переходите на приложение-сканер.";
const NO_SCANNER_TEXT = "Обновите Telegram — сканер QR недоступен в этой версии. Ищите делегата по фамилии ниже.";

const STATUS_TONE = {
  new: "success",
  moved: "success",
  duplicate: "warn",
  denied: "error",
  not_found: "error",
  foreign_event: "error",
  wrong_city: "error",
  invalid_point: "error",
  onsite_off: "error",
  rejected: "error",
  undone: "warn",
  undo_refused: "error",
  // вход не в день форума делегата: ничего не записано, волонтёру — проверить (проба накануне,
  // делегат другого города у стойки без привязки)
  not_forum_day: "warn",
};
const STATUS_HEADING = {
  new: "Отмечен",
  denied: "Не пропущен",
  not_found: "Не пропущен",
  foreign_event: "Не пропущен",
  wrong_city: "Не пропущен",
  invalid_point: "Не пропущен",
  onsite_off: "Не пропущен",
  rejected: "Не пропущен",
  not_forum_day: "Не отмечен",
};
// Запасные подписи регистрации на месте — только если /checkin/net-texts не дошёл.
const ONSITE_FALLBACK = {
  onsite_approve_button_text: "✅ Пропустить и одобрить",
  onsite_approve_confirm_text: "Одобрить {name} на месте и отметить вход? Решение запишется на вас.",
  onsite_register_button_text: "📝 Зарегистрировать на месте",
  onsite_pending_title_text: "📝 Ждут на стойке",
  onsite_override_button_text: "⚠️ Пропустить вопреки отказу",
  onsite_remove_button_text: "🗑 Убрать",
  onsite_remove_confirm_text: "Убрать {name} из списка ждущих? Короткая анкета удалится — если человек всё же придёт, ему нужно будет заполнить её заново.",
  onsite_move_confirm_text: "Человек записан на форум в другом городе — после одобрения он переедет в {city}.",
  onsite_override_confirm_text: "Заявку {name} отклонил менеджер. Пропустить вопреки отказу и отметить вход? Отказ будет отменён, решение запишется на вас и попадёт в журнал.",
};
const HAPTIC_BY_TONE = { success: "success", warn: "warning", error: "error" };
// D-18..D-20: и «new», и «moved» — успешная отметка (попап остаётся открытым, продолжаем
// сканировать) — «moved» просто означает, что делегат перешёл с одной параллельной сессии
// слота на другую, это не отказ и не дубль.
const SUCCESS_STATUSES = new Set(["new", "moved"]);

// Сеть не ответила (fetch упал до HTTP-статуса) — ApiError всегда несёт число в `.status`,
// «сырой» TypeError браузера — нет; тот же приём различения, что нужен только этому экрану
// (остальные экраны молча используют общий screenText("network_error") без развилки).
function isNetworkError(err) {
  return !(err && typeof err.status === "number");
}

function failureText(err, fallback) {
  if (err && err.timeout) return TIMEOUT_TEXT;
  return isNetworkError(err) ? NETWORK_TEXT : errorText(err, fallback);
}

function timeOnly(stamp) {
  if (!stamp) return "";
  const text = String(stamp);
  const spaceIdx = text.indexOf(" ");
  return (spaceIdx >= 0 ? text.slice(spaceIdx + 1) : text).slice(0, 5);
}

// Подписка на закрытие попапа камеры снимается при уходе с экрана (как в form.js).
let tgRef = null;
let popupClosedHandler = null;

export function unmount() {
  if (tgRef && popupClosedHandler && typeof tgRef.offEvent === "function") {
    tgRef.offEvent("scanQrPopupClosed", popupClosedHandler);
  }
  tgRef = null;
  popupClosedHandler = null;
}

export async function render(root, params, ctx) {
  const { h, api } = ctx;

  const { el: notice, say } = noticeBox(h);
  const statsBox = h("div", { class: "checkin-stats" }, h("span", { class: "muted", text: "Загрузка…" }));
  const plaque = h("div", { class: "checkin-plaque hidden" });
  const scanBtn = h("button", { class: "btn", type: "button", text: "📷 Сканировать" });
  const searchInput = h("input", { class: "input", type: "text", placeholder: "Фамилия делегата" });
  const searchResults = h("div", { class: "flat-list" });
  const fallbackNote = h("p", { class: "muted hidden", text: NO_SCANNER_TEXT });
  const citySelect = h("select", { class: "input hidden" });
  const cityField = h("div", { class: "field hidden" }, h("label", { text: "Город" }), citySelect);
  const pointChips = h("div", { class: "flat-list" }, h("span", { class: "muted", text: "Загрузка…" }));
  const pointCounter = h("div", { class: "muted checkin-point-counter" });

  // Идея №11: полоса «сеть медленная». Тексты — с сервера (/checkin/net-texts, в переводе),
  // берутся при открытии экрана; если тогда не дошли — дозапрашиваются после первой удачной
  // отметки. Решение «показать/скрыть» — net_health.js по времени ответа скана/отметки.
  const netText = h("span", { class: "checkin-net-banner-text", text: "⚠️" });
  const netHelp = h("div", { class: "checkin-net-banner-help hidden" });
  const netHelpBtn = h("button", { class: "btn secondary checkin-net-banner-how hidden", type: "button" });
  netHelpBtn.addEventListener("click", () => netHelp.classList.toggle("hidden"));
  const netBanner = h("div", { class: "checkin-net-banner hidden", role: "status" },
    h("div", { class: "checkin-net-banner-row" }, netText, netHelpBtn),
    netHelp,
  );
  const netHealth = createNetHealth();
  let netTextsLoaded = false;
  let onsiteTexts = {};

  async function loadNetTexts() {
    if (netTextsLoaded) return;
    try {
      const t = await api("/checkin/net-texts");
      netTextsLoaded = true;
      onsiteTexts = t.onsite || {};
      applyOnsite();
      if (t.text) netText.textContent = `⚠️ ${t.text}`;
      if (t.help_label && t.help_text) {
        netHelpBtn.textContent = t.help_label;
        netHelp.textContent = t.help_text;
        netHelpBtn.classList.remove("hidden");
      }
    } catch (err) {
      // сеть уже плохая — полоса останется с одним значком, тексты дозапросим позже
    }
  }

  function onNetChange(degraded) {
    netBanner.classList.toggle("hidden", !degraded);
    if (!degraded) netHelp.classList.add("hidden");
  }

  function measured(fn) {
    return timed(netHealth, fn, onNetChange);
  }

  // ── регистрация на месте (D-41): подписи, блок «Ждут на стойке» ─────────────────────────
  let onsiteEnabled = false;
  let onsiteCanApprove = false; // право «Одобрять на месте» (onsite_can_approve из /points)
  let onsiteBusy = false;

  function ot(key) {
    return onsiteTexts[key] || ONSITE_FALLBACK[key] || "";
  }

  const onsiteTitle = h("h2", { text: ONSITE_FALLBACK.onsite_pending_title_text });
  const onsiteList = h("div", { class: "flat-list" });
  const onsiteRegBtn = h("button", { class: "btn secondary", type: "button", onClick: () => showWalkinQr() });
  const onsiteRefresh = h("button", {
    class: "btn secondary", type: "button", text: "🔄 Обновить", onClick: () => loadOnsitePending(),
  });
  const onsiteSearch = h("input", { class: "input", type: "text", placeholder: "Фамилия в списке" });
  const onsiteCount = h("div", { class: "muted" });
  const onsiteMore = h("button", {
    class: "btn secondary hidden", type: "button", text: "Показать ещё", onClick: () => loadOnsitePending(true),
  });
  let onsiteSearchTimer = null;
  onsiteSearch.addEventListener("input", () => {
    if (onsiteSearchTimer) clearTimeout(onsiteSearchTimer);
    onsiteSearchTimer = setTimeout(() => loadOnsitePending(), SEARCH_DEBOUNCE_MS);
  });
  const onsiteBox = h("div", { class: "hidden" },
    h("div", { class: "task-actions" }, onsiteRegBtn),
    onsiteTitle,
    h("div", { class: "field" }, onsiteSearch),
    h("div", { class: "task-actions" }, onsiteRefresh),
    onsiteCount,
    onsiteList,
    h("div", { class: "task-actions" }, onsiteMore),
  );

  function applyOnsite() {
    onsiteBox.classList.toggle("hidden", !onsiteEnabled);
    onsiteRegBtn.textContent = ot("onsite_register_button_text");
    onsiteTitle.textContent = ot("onsite_pending_title_text");
  }

  function onsiteAllowed() {
    return onsiteEnabled && selectedPoint !== TRAINING_POINT;
  }

  function cityParam() {
    return encodeURIComponent(citySelect.value || "");
  }

  root.append(
    h("h1", { text: "Сканер" }),
    netBanner,
    statsBox,
    notice,
    cityField,
    h("div", { class: "field" },
      h("label", { text: "Точка" }),
      pointChips,
      pointCounter,
    ),
    h("div", { class: "task-actions" }, scanBtn),
    fallbackNote,
    plaque,
    h("h2", { text: "Поиск по фамилии" }),
    h("div", { class: "field" }, searchInput),
    searchResults,
    onsiteBox,
  );

  async function loadStats() {
    let stats;
    try {
      stats = await api("/checkin/stats");
    } catch (err) {
      statsBox.replaceChildren(h("span", {
        class: "muted", text: isNetworkError(err) ? NETWORK_TEXT : "Счётчик недоступен.",
      }));
      return;
    }
    if (stats.cities) {
      const rows = stats.cities.map((c) => h("div", { text: `${c.label}: пришли ${c.arrived} из ${c.approved}` }));
      // «Итого» — только когда городов больше одного (один город итог лишь повторяет).
      if (rows.length > 1) rows.push(h("div", { class: "checkin-stats-total", text: `Итого: ${stats.arrived} из ${stats.approved}` }));
      if (stats.today) rows.unshift(h("div", { text: "Сегодня:" }));
      statsBox.replaceChildren(...rows);
    } else {
      statsBox.replaceChildren(h("span", { text: `${stats.today ? "Сегодня пришли" : "Пришли"}: ${stats.arrived} из ${stats.approved} одобренных` }));
    }
  }

  // ── точки отметки (D-18): «Вход» + сессии СЕГОДНЯ, «идёт сейчас» первыми ────────────────
  let selectedPoint = ENTRY_POINT;
  let pointsData = [];

  function renderPointCounter() {
    const current = pointsData.find((pt) => pt.point === selectedPoint);
    if (!current) { pointCounter.textContent = ""; return; }
    // Бэклог чек-ина №7: у «🧪 Тренировка» счётчика нет — вместо него пометка с сервера.
    if (current.count == null) { pointCounter.textContent = current.note || ""; return; }
    const countText = current.capacity ? `${current.count} из ${current.capacity}` : String(current.count);
    pointCounter.textContent = `Отмечено на точке: ${countText}`;
  }

  function renderPointChips() {
    if (pointsData.length === 0) {
      pointChips.replaceChildren(h("span", { class: "muted", text: "Точки недоступны." }));
      return;
    }
    const chips = pointsData.map((pt) => {
      const isSel = pt.point === selectedPoint;
      const countText = pt.count == null ? ""
        : ` · ${pt.capacity ? `${pt.count} из ${pt.capacity}` : String(pt.count)}`;
      const dot = pt.live ? "🔴 " : "";
      return h("button", {
        class: `chip-choice${isSel ? " chosen" : ""}`, type: "button",
        text: `${dot}${pt.label}${countText}`,
        onClick: () => { selectedPoint = pt.point; renderPointChips(); },
      });
    });
    pointChips.replaceChildren(...chips);
    renderPointCounter();
  }

  async function loadPoints(cityCode) {
    let data;
    try {
      data = await api(cityCode
        ? `/checkin/points?city=${encodeURIComponent(cityCode)}`
        : "/checkin/points");
    } catch (err) {
      pointChips.replaceChildren(h("span", {
        class: "muted", text: isNetworkError(err) ? NETWORK_TEXT : "Точки недоступны.",
      }));
      return;
    }
    if (data.cities) {
      cityField.classList.remove("hidden");
      citySelect.replaceChildren(
        h("option", { value: "", text: "Выберите город" }),
        ...data.cities.map((c) => h("option", { value: c.code, text: c.label })),
      );
      if (data.city) citySelect.value = data.city;
    } else {
      cityField.classList.add("hidden");
    }
    pointsData = data.points || [];
    if (!pointsData.some((pt) => pt.point === selectedPoint)) selectedPoint = ENTRY_POINT;
    renderPointChips();
    onsiteEnabled = Boolean(data.onsite_enabled);
    onsiteCanApprove = Boolean(data.onsite_can_approve);
    applyOnsite();
  }

  citySelect.addEventListener("change", async () => {
    if (!citySelect.value) return;
    await loadPoints(citySelect.value);
    await loadOnsitePending();
  });

  const SCAN_POPUP_TEXT = "Зелёная вибрация — отмечен. Иначе окно закроется";

  // Попап камеры открыт: при 🟢 он остаётся поверх плашки — кнопку «↩️ Отменить» под ним не
  // видно, поэтому её отсчёт стартует, когда попап закрыт.
  let popupOpen = false;

  function startScan() {
    if (!canScan) return;
    scanGate.resume(); // волонтёр видел отказ и сам продолжил — очередь уходит дальше
    popupOpen = true;
    tg.showScanQrPopup({ text: SCAN_POPUP_TEXT }, onQrText);
  }

  // closeButton=true — не-🟢 исход (duplicate/denied/not_found/foreign_event/другой город
  // форума/сетевая ошибка): родной попап уже закрыт (submitScan вызвал tg.closeScanQrPopup()
  // до этого показа), плашка получает крупную кнопку «Сканировать дальше», заново открывающую
  // попап.
  // Идея №32: «↩️ Отменить» — только у своей только что поставленной отметки (сервер отдаёт
  // `res.undo` лишь для new/moved), кнопка живёт `undo.seconds` секунд. Таймер — удобство, не
  // защита: окно, «своя» и «последняя» проверяются на сервере (/checkin/undo).
  let undoTimer = null;
  let pendingUndo = null; // кнопка, отрисованная под открытым попапом: { btn, seconds, acceptUntil }

  function startUndoCountdown(btn, seconds) {
    if (undoTimer) clearTimeout(undoTimer);
    undoTimer = setTimeout(() => { btn.remove(); undoTimer = null; }, (seconds || 10) * 1000);
  }

  function onScanPopupClosed() {
    popupOpen = false;
    if (!pendingUndo) return;
    const { btn, seconds, acceptUntil } = pendingUndo;
    pendingUndo = null;
    if (!btn.isConnected) return;
    if (Date.now() >= acceptUntil) { btn.remove(); return; } // сервер уже не примет
    startUndoCountdown(btn, seconds);
    if (typeof plaque.scrollIntoView === "function") plaque.scrollIntoView({ block: "center" });
  }

  function undoButton(undo, res) {
    const btn = h("button", { class: "btn secondary checkin-plaque-undo", type: "button", text: undo.label });
    btn.addEventListener("click", async () => {
      if (btn.hasAttribute("disabled")) return;
      btn.setAttribute("disabled", "");
      if (undoTimer) { clearTimeout(undoTimer); undoTimer = null; }
      pendingUndo = null;
      // Тренировка: отметки нет — показываем, как выглядит отмена, без запроса на сервер.
      if (undo.demo) {
        showPlaque({
          status: "undone", reason_text: undo.text, full_name: res.full_name, city: res.city, city_label: res.city_label,
          training_note: res.training_note,
        }, { closeButton: true });
        return;
      }
      let res;
      try {
        res = await api("/checkin/undo", { method: "POST", body: { id: undo.id } });
      } catch (err) {
        res = { status: "error", reason_text: isNetworkError(err) ? NETWORK_TEXT : errorText(err, "Не получилось отменить.") };
      }
      showPlaque(res, { closeButton: true });
      await loadStats();
      await loadPoints(citySelect.value || undefined);
    });
    if (popupOpen && popupClosedHandler) { // старый клиент без события закрытия — отсчёт сразу
      const acceptMs = (undo.valid_seconds || undo.seconds || 10) * 1000;
      pendingUndo = { btn, seconds: undo.seconds, acceptUntil: Date.now() + acceptMs };
    } else {
      startUndoCountdown(btn, undo.seconds);
    }
    return btn;
  }

  function showPlaque(res, { closeButton = false } = {}) {
    if (undoTimer) { clearTimeout(undoTimer); undoTimer = null; }
    pendingUndo = null;
    const tone = STATUS_TONE[res.status] || "error";
    plaque.className = `checkin-plaque tone-${tone}`;
    const dot = tone === "success" ? "🟢" : tone === "warn" ? "🟡" : "🔴";
    const heading = res.status === "undone" || res.status === "undo_refused"
      ? res.reason_text
      : res.status === "duplicate"
      ? `Уже был в ${timeOnly(res.scanned_at)}`
      : res.status === "moved"
      ? `Перенесено${res.previous_title ? ` с «${res.previous_title}»` : ""}`
      : (STATUS_HEADING[res.status] || "Не пропущен");
    const nextBtn = h("button", { class: "btn checkin-plaque-next", type: "button", text: "📷 Сканировать дальше" });
    nextBtn.addEventListener("click", () => {
      plaque.classList.add("hidden");
      startScan();
    });
    plaque.replaceChildren(...[
      h("div", { class: "checkin-plaque-dot", text: dot }),
      h("div", { class: "checkin-plaque-heading", text: heading }),
      res.full_name ? h("div", { class: "checkin-plaque-name", text: res.full_name }) : null,
      res.city_label
        ? h("div", { class: res.city_emphasis ? "checkin-plaque-city checkin-plaque-city-big" : "checkin-plaque-city", text: res.city_label })
        : null,
      res.reason_text && heading !== res.reason_text
        ? h("div", { class: "checkin-plaque-reason", text: res.reason_text }) : null,
      res.hint ? h("div", { class: "checkin-plaque-reason", text: res.hint }) : null,
      res.training_note ? h("div", { class: "checkin-plaque-city", text: `🧪 ${res.training_note}` }) : null,
      res.undo ? undoButton(res.undo, res) : null,
      res.onsite_approve && res.telegram_id && selectedPoint !== TRAINING_POINT
        ? onsiteApproveButton({ telegram_id: res.telegram_id, full_name: res.full_name, onsite_move_to: res.onsite_move_to }) : null,
      res.onsite_override && res.telegram_id && selectedPoint !== TRAINING_POINT
        ? onsiteApproveButton({ telegram_id: res.telegram_id, full_name: res.full_name }, { override: true }) : null,
      res.onsite_register && selectedPoint !== TRAINING_POINT ? onsiteRegisterButton() : null,
      res.day_override && res.telegram_id && selectedPoint !== TRAINING_POINT ? dayOverrideButton(res) : null,
      closeButton ? nextBtn : null,
    ].filter(Boolean));
    haptic(HAPTIC_BY_TONE[tone] || "error");
  }

  // Пока ждём ответа на скан (на медленном Wi-Fi — до нескольких секунд), на экране «проверяю»,
  // а не плашка предыдущего человека: волонтёр не примет чужой результат за этот.
  function showChecking() {
    if (undoTimer) { clearTimeout(undoTimer); undoTimer = null; }
    pendingUndo = null;
    plaque.className = "checkin-plaque";
    plaque.replaceChildren(
      h("div", { class: "checkin-plaque-dot", text: "⏳" }),
      h("div", { class: "checkin-plaque-heading", text: "Проверяю…" }),
    );
  }

  // Дата форума в настройках неверна — менеджер отмечает «всё равно» (сервер проверяет право).
  function dayOverrideButton(res) {
    const btn = h("button", { class: "btn secondary", type: "button", text: "⚠️ Отметить всё равно" });
    btn.addEventListener("click", async () => {
      if (btn.hasAttribute("disabled")) return;
      const ok = await askConfirm(`Отметить вход ${res.full_name || "делегата"}, хотя по настройкам у делегата сегодня нет форума? Если дата форума указана неверно — поправьте её в админке.`);
      if (!ok) return;
      btn.setAttribute("disabled", "");
      try {
        const out = await api("/checkin/manual", {
          method: "POST", timeoutMs: SCAN_TIMEOUT_MS,
          body: { telegram_id: res.telegram_id, point: selectedPoint, city: citySelect.value || undefined, force_day: true },
        });
        showPlaque(out, { closeButton: true });
        refreshCounters();
      } catch (err) {
        btn.removeAttribute("disabled");
        say(failureText(err, "Не получилось отметить — попробуйте ещё раз."), "warn");
      }
    });
    return btn;
  }

  function closeScanPopup() {
    popupOpen = false;
    if (tg && typeof tg.closeScanQrPopup === "function") tg.closeScanQrPopup();
  }

  // Счётчики (шапка, чипы точек) — фоном и с дебаунсом: серия сканов даёт одну пару запросов,
  // а очередной скан не ждёт их ответа (иначе QR следующего делегата терялся).
  let countersTimer = null;
  function refreshCounters() {
    if (countersTimer) clearTimeout(countersTimer);
    countersTimer = setTimeout(() => {
      countersTimer = null;
      loadStats();
      loadPoints(citySelect.value || undefined);
    }, COUNTERS_DEBOUNCE_MS);
  }

  // Под плашкой отказа: QR, пойманные во время отправки, ждут «Сканировать дальше».
  function noteQueued() {
    const n = scanGate.pending().length;
    if (n) plaque.append(h("div", { class: "checkin-plaque-city", text: `Ещё ${n} QR в очереди — отметятся после «Сканировать дальше».` }));
  }

  // Завершается по ответу на сам скан — следующий QR из очереди уходит сразу. Не-🟢 исход
  // возвращает HOLD: очередь ждёт явного «Сканировать дальше», плашку отказа не затирает.
  async function submitScan(payloadText) {
    showChecking();
    try {
      const res = await measured(() => api("/checkin/scan", {
        method: "POST", timeoutMs: SCAN_TIMEOUT_MS,
        body: { payload: payloadText, point: selectedPoint, city: citySelect.value || undefined },
      }));
      loadNetTexts();
      const isSuccess = SUCCESS_STATUSES.has(res.status);
      if (!isSuccess) closeScanPopup(); // 🟡/🔴 — родной попап закрывается, плашка даёт «дальше»
      showPlaque(res, { closeButton: !isSuccess });
      refreshCounters();
      if (isSuccess) return undefined;
    } catch (err) {
      closeScanPopup(); // сетевая ошибка, таймаут, любая другая — тоже 🔴, попап закрывается
      const text = failureText(err, "Не получилось отметить — попробуйте ещё раз.");
      showPlaque({ status: "error", reason_text: text }, { closeButton: true });
    }
    noteQueued();
    return HOLD;
  }

  // ── скан QR: непрерывный режим ────────────────────────────────────────────────────────
  // Синхронный колбэк не знает исход — попап закрывает submitScan() сам (возвращаем false).
  const scanGate = createScanGate({ submit: submitScan });
  const onQrText = (text) => scanGate.onText(text);

  const tg = window.Telegram && window.Telegram.WebApp;
  const canScan = Boolean(tg && typeof tg.showScanQrPopup === "function");
  if (!canScan) {
    scanBtn.setAttribute("disabled", "");
    fallbackNote.className = "muted";
  }
  scanBtn.addEventListener("click", startScan);
  if (tg && typeof tg.onEvent === "function") {
    tg.onEvent("scanQrPopupClosed", onScanPopupClosed);
    popupClosedHandler = onScanPopupClosed;
    tgRef = tg;
  }

  // ── поиск по фамилии (D-11/D-12) ──────────────────────────────────────────────────────
  let searchTimer = null;

  function resultRow(person) {
    const metaBase = [person.city_label, atUsername(person.username), person.university]
      .filter(Boolean).join(" · ") || "—";
    const meta = person.eligible ? metaBase : `${metaBase} — ${person.reason_text || "не допущен"}`;
    if (!person.eligible && person.onsite_approve && onsiteAllowed()) {
      return flatRow(h, { title: person.full_name, meta, trailing: onsiteApproveButton(person) });
    }
    if (!person.eligible && person.onsite_override && onsiteAllowed()) {
      return flatRow(h, { title: person.full_name, meta, trailing: onsiteApproveButton(person, { override: true }) });
    }
    if (person.onsite_register && onsiteAllowed()) {
      return flatRow(h, { title: person.full_name, meta, trailing: onsiteRegisterButton() });
    }
    const btn = h("button", { class: "btn secondary", type: "button", text: "Отметить" });
    if (!person.eligible) btn.setAttribute("disabled", "");
    btn.addEventListener("click", async () => {
      if (btn.hasAttribute("disabled")) return;
      btn.setAttribute("disabled", "");
      try {
        const res = await measured(() => api("/checkin/manual", {
          method: "POST", timeoutMs: SCAN_TIMEOUT_MS, body: { telegram_id: person.telegram_id, point: selectedPoint, city: citySelect.value || undefined },
        }));
        loadNetTexts();
        showPlaque(res);
        await loadStats();
        await loadPoints(citySelect.value || undefined);
      } catch (err) {
        say(failureText(err, "Не получилось отметить — попробуйте ещё раз."), "warn");
      } finally {
        btn.removeAttribute("disabled");
      }
    });
    return flatRow(h, { title: person.full_name, meta, trailing: btn });
  }

  async function search(q) {
    const needle = q.trim();
    searchResults.replaceChildren();
    if (needle.length < 2) return;
    let items;
    try {
      const page = await api(`/checkin/search?q=${encodeURIComponent(needle)}&city=${cityParam()}`);
      items = page.items;
    } catch (err) {
      searchResults.append(h("p", {
        class: "error-inline",
        text: isNetworkError(err) ? NETWORK_TEXT : "Не удалось найти — попробуйте ещё раз.",
      }));
      return;
    }
    if (searchInput.value.trim() !== needle) return; // ушли дальше, пока грузили
    if (items.length === 0) {
      searchResults.append(h("p", { class: "muted", text: "Никого не нашли — проверьте написание." }));
      if (onsiteAllowed()) searchResults.append(h("div", { class: "task-actions" }, onsiteRegisterButton()));
      return;
    }
    searchResults.append(...items.map(resultRow));
  }

  searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    const value = searchInput.value;
    searchTimer = setTimeout(() => search(value), SEARCH_DEBOUNCE_MS);
  });

  // ── регистрация на месте (D-41) ──────────────────────────────────────────────────────
  function askConfirm(text) {
    return new Promise((resolve) => {
      if (tg && typeof tg.showConfirm === "function") {
        try {
          tg.showConfirm(text, (ok) => resolve(Boolean(ok)));
          return;
        } catch (err) {
          // старый клиент Telegram — ниже обычный confirm
        }
      }
      resolve(window.confirm(text));
    });
  }

  // Одобрение ОДНОГО человека у стойки: всегда через подтверждение с именем, кнопка
  // заблокирована на время запроса (двойной тап). Решение и отметку пишет сервер.
  // override — заявку отклонил менеджер: своё подтверждение («отказ будет отменён») и флаг
  // override_reject, без него сервер отклонённую заявку не одобряет.
  async function approveOnsite(person, btn, override = false) {
    if (onsiteBusy) return;
    const confirmKey = override ? "onsite_override_confirm_text" : "onsite_approve_confirm_text";
    let question = ot(confirmKey).replace("{name}", person.full_name || "—");
    // walk-in «не того» города: одобрение переведёт его в город стойки — говорим это прямо.
    if (person.onsite_move_to) question += `\n\n${ot("onsite_move_confirm_text").replace("{city}", person.onsite_move_to)}`;
    const ok = await askConfirm(question);
    if (!ok) return;
    onsiteBusy = true;
    if (btn) btn.setAttribute("disabled", "");
    try {
      const res = await measured(() => api("/checkin/onsite/approve", {
        method: "POST",
        body: { telegram_id: person.telegram_id, city: citySelect.value || undefined, override_reject: override },
      }));
      showPlaque(res, { closeButton: true });
      await loadStats();
      await loadPoints(citySelect.value || undefined);
      await loadOnsitePending();
    } catch (err) {
      say(isNetworkError(err) ? NETWORK_TEXT : errorText(err, "Не получилось одобрить — попробуйте ещё раз."), "warn");
    } finally {
      onsiteBusy = false;
      if (btn) btn.removeAttribute("disabled");
    }
  }

  function onsiteApproveButton(person, { override = false } = {}) {
    const label = ot(override ? "onsite_override_button_text" : "onsite_approve_button_text");
    const btn = h("button", { class: override ? "btn secondary" : "btn", type: "button", text: label });
    btn.addEventListener("click", () => {
      if (btn.hasAttribute("disabled")) return;
      approveOnsite(person, btn, override);
    });
    return btn;
  }

  function onsiteRegisterButton() {
    return h("button", {
      class: "btn secondary", type: "button", text: ot("onsite_register_button_text"),
      onClick: () => showWalkinQr(),
    });
  }

  // QR короткой анкеты своего города — для камеры телефона человека. Картинка приходит
  // data URI внутри JSON: отдельный URL картинки получил бы 401 (тег img не шлёт initData).
  async function showWalkinQr() {
    let res;
    try {
      res = await measured(() => api(`/checkin/onsite/link?city=${cityParam()}`));
    } catch (err) {
      say(isNetworkError(err) ? NETWORK_TEXT : errorText(err, "Не получилось показать QR — попробуйте ещё раз."), "warn");
      return;
    }
    if (!res.url || !res.qr) {
      say(res.reason_text || "Не получилось показать QR — попробуйте ещё раз.", "warn");
      return;
    }
    if (undoTimer) { clearTimeout(undoTimer); undoTimer = null; }
    const closeBtn = h("button", {
      class: "btn secondary checkin-plaque-next", type: "button", text: "Закрыть",
      onClick: () => plaque.classList.add("hidden"),
    });
    plaque.className = "checkin-plaque checkin-onsite-qr";
    plaque.replaceChildren(...[
      h("div", { class: "checkin-plaque-heading", text: ot("onsite_register_button_text") }),
      h("img", { class: "checkin-onsite-qr-img", src: res.qr, alt: "QR" }),
      h("div", { class: "checkin-plaque-city checkin-onsite-qr-url", text: res.url }),
      res.hint ? h("div", { class: "checkin-plaque-reason", text: res.hint }) : null,
      closeBtn,
    ].filter(Boolean));
    if (typeof plaque.scrollIntoView === "function") plaque.scrollIntoView({ block: "center" });
  }

  // «Ждут на стойке»: при открытии экрана, после смены города и после каждого одобрения —
  // без таймера-опроса (сеть на площадке слабая), плюс кнопка «Обновить».
  // Постранично (сервер отдаёт total/next_offset) и с поиском по фамилии: при сотнях коротких
  // анкет у стойки старые не пропадают молча. append=true — «Показать ещё» дописывает страницу.
  let onsiteOffset = 0;

  function onsiteRow(it, canApprove) {
    const actions = canApprove
      ? h("div", { class: "task-actions" }, onsiteApproveButton(it), onsiteRemoveButton(it))
      : null;
    return flatRow(h, {
      title: it.full_name,
      meta: [it.university, atUsername(it.username), it.registered_at].filter(Boolean).join(" · ") || "—",
      trailing: actions,
    });
  }

  async function loadOnsitePending(append = false) {
    if (!onsiteEnabled) { onsiteList.replaceChildren(); onsiteMore.classList.add("hidden"); return; }
    const offset = append ? onsiteOffset : 0;
    const q = encodeURIComponent(onsiteSearch.value.trim());
    let page;
    try {
      page = await api(`/checkin/onsite/pending?city=${cityParam()}&q=${q}&offset=${offset}`);
    } catch (err) {
      onsiteList.replaceChildren(h("p", {
        class: "error-inline",
        text: isNetworkError(err) ? NETWORK_TEXT : "Список недоступен — нажмите «Обновить».",
      }));
      return;
    }
    const items = page.items || [];
    const canApprove = Boolean(page.can_approve);
    onsiteOffset = page.next_offset || 0;
    onsiteMore.classList.toggle("hidden", page.next_offset == null);
    onsiteCount.textContent = page.total ? `Всего: ${page.total}` : "";
    if (!append && items.length === 0) {
      onsiteList.replaceChildren(h("p", { class: "muted", text: onsiteSearch.value.trim() ? "Никого не нашли" : "Пока никого" }));
      return;
    }
    const rows = items.map((it) => onsiteRow(it, canApprove));
    if (append) onsiteList.append(...rows);
    else onsiteList.replaceChildren(...rows);
  }

  // «Убрать» короткую анкету (случайная, дубль, человек ушёл): подтверждение с именем, строку
  // удаляет сервер (только walk-in без решения своего города).
  async function removeWalkin(person, btn) {
    if (onsiteBusy) return;
    const ok = await askConfirm(ot("onsite_remove_confirm_text").replace("{name}", person.full_name || "—"));
    if (!ok) return;
    onsiteBusy = true;
    if (btn) btn.setAttribute("disabled", "");
    try {
      const res = await measured(() => api("/checkin/onsite/remove", {
        method: "POST", body: { telegram_id: person.telegram_id, city: citySelect.value || undefined },
      }));
      if (res.status !== "removed") say(res.reason_text || "Убрать не получилось.", "warn");
      await loadOnsitePending();
    } catch (err) {
      say(isNetworkError(err) ? NETWORK_TEXT : errorText(err, "Не получилось убрать — попробуйте ещё раз."), "warn");
    } finally {
      onsiteBusy = false;
      if (btn) btn.removeAttribute("disabled");
    }
  }

  function onsiteRemoveButton(person) {
    const btn = h("button", { class: "btn secondary", type: "button", text: ot("onsite_remove_button_text") });
    btn.addEventListener("click", () => {
      if (btn.hasAttribute("disabled")) return;
      removeWalkin(person, btn);
    });
    return btn;
  }

  await loadNetTexts();
  await loadStats();
  await loadPoints();
  await loadOnsitePending();
}
