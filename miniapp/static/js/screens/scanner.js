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
// много раз за секунды, пока волонтёр не отвёл камеру — `RESCAN_GUARD_MS` глушит повторы
// того же текста, а не блокирует скан вовсе (другой делегат сканируется сразу).

import { flatRow, errorText, noticeBox } from "../ui.js";
import { haptic } from "../motion.js";

const ENTRY_POINT = "entry";
const SEARCH_DEBOUNCE_MS = 300;
const RESCAN_GUARD_MS = 3000;

const NETWORK_TEXT = "Нет связи — переходите на приложение-сканер.";
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
};
const STATUS_HEADING = {
  new: "Отмечен",
  denied: "Не пропущен",
  not_found: "Не пропущен",
  foreign_event: "Не пропущен",
  wrong_city: "Не пропущен",
  invalid_point: "Не пропущен",
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

function timeOnly(stamp) {
  if (!stamp) return "";
  const text = String(stamp);
  const spaceIdx = text.indexOf(" ");
  return (spaceIdx >= 0 ? text.slice(spaceIdx + 1) : text).slice(0, 5);
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

  root.append(
    h("h1", { text: "Сканер" }),
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
      rows.push(h("div", { class: "checkin-stats-total", text: `Итого: ${stats.arrived} из ${stats.approved}` }));
      statsBox.replaceChildren(...rows);
    } else {
      statsBox.replaceChildren(h("span", { text: `Пришли: ${stats.arrived} из ${stats.approved} одобренных` }));
    }
  }

  // ── точки отметки (D-18): «Вход» + сессии СЕГОДНЯ, «идёт сейчас» первыми ────────────────
  let selectedPoint = ENTRY_POINT;
  let pointsData = [];

  function renderPointCounter() {
    const current = pointsData.find((pt) => pt.point === selectedPoint);
    if (!current) { pointCounter.textContent = ""; return; }
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
      const countText = pt.capacity ? `${pt.count} из ${pt.capacity}` : String(pt.count);
      const dot = pt.live ? "🔴 " : "";
      return h("button", {
        class: `chip-choice${isSel ? " chosen" : ""}`, type: "button",
        text: `${dot}${pt.label} · ${countText}`,
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
  }

  citySelect.addEventListener("change", () => {
    if (citySelect.value) loadPoints(citySelect.value);
  });

  const SCAN_POPUP_TEXT = "Зелёная вибрация — отмечен. Иначе окно закроется";

  function startScan() {
    if (!canScan) return;
    tg.showScanQrPopup({ text: SCAN_POPUP_TEXT }, onQrText);
  }

  // closeButton=true — не-🟢 исход (duplicate/denied/not_found/foreign_event/другой город
  // форума/сетевая ошибка): родной попап уже закрыт (submitScan вызвал tg.closeScanQrPopup()
  // до этого показа), плашка получает крупную кнопку «Сканировать дальше», заново открывающую
  // попап.
  function showPlaque(res, { closeButton = false } = {}) {
    const tone = STATUS_TONE[res.status] || "error";
    plaque.className = `checkin-plaque tone-${tone}`;
    const dot = tone === "success" ? "🟢" : tone === "warn" ? "🟡" : "🔴";
    const heading = res.status === "duplicate"
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
      res.city ? h("div", { class: "checkin-plaque-city", text: res.city }) : null,
      res.reason_text ? h("div", { class: "checkin-plaque-reason", text: res.reason_text }) : null,
      closeButton ? nextBtn : null,
    ].filter(Boolean));
    haptic(HAPTIC_BY_TONE[tone] || "error");
  }

  function closeScanPopup() {
    if (tg && typeof tg.closeScanQrPopup === "function") tg.closeScanQrPopup();
  }

  let scanBusy = false;
  async function submitScan(payloadText) {
    if (scanBusy) return;
    scanBusy = true;
    try {
      const res = await api("/checkin/scan", { method: "POST", body: { payload: payloadText, point: selectedPoint } });
      const isSuccess = SUCCESS_STATUSES.has(res.status);
      if (!isSuccess) closeScanPopup(); // 🟡/🔴 — родной попап закрывается, плашка даёт «дальше»
      showPlaque(res, { closeButton: !isSuccess });
      await loadStats();
      await loadPoints(citySelect.value || undefined);
    } catch (err) {
      closeScanPopup(); // сетевая/любая другая ошибка — тоже 🔴, попап закрывается
      const text = isNetworkError(err) ? NETWORK_TEXT : errorText(err, "Не получилось отметить — попробуйте ещё раз.");
      showPlaque({ status: "error", reason_text: text }, { closeButton: true });
    } finally {
      scanBusy = false;
    }
  }

  // ── скан QR: непрерывный режим ────────────────────────────────────────────────────────
  let lastText = null;
  let lastAt = 0;
  function onQrText(text) {
    const now = Date.now();
    if (text === lastText && now - lastAt < RESCAN_GUARD_MS) return false;
    lastText = text;
    lastAt = now;
    submitScan(text);
    return false; // синхронный колбэк не знает исход — попап закрывает submitScan() сам
  }

  const tg = window.Telegram && window.Telegram.WebApp;
  const canScan = Boolean(tg && typeof tg.showScanQrPopup === "function");
  if (!canScan) {
    scanBtn.setAttribute("disabled", "");
    fallbackNote.className = "muted";
  }
  scanBtn.addEventListener("click", startScan);

  // ── поиск по фамилии (D-11/D-12) ──────────────────────────────────────────────────────
  let searchTimer = null;

  function resultRow(person) {
    const metaBase = [person.city, person.username ? `@${person.username}` : null, person.university]
      .filter(Boolean).join(" · ") || "—";
    const meta = person.eligible ? metaBase : `${metaBase} — ${person.reason_text || "не допущен"}`;
    const btn = h("button", { class: "btn secondary", type: "button", text: "Отметить" });
    if (!person.eligible) btn.setAttribute("disabled", "");
    btn.addEventListener("click", async () => {
      if (btn.hasAttribute("disabled")) return;
      btn.setAttribute("disabled", "");
      try {
        const res = await api("/checkin/manual", {
          method: "POST", body: { telegram_id: person.telegram_id, point: selectedPoint },
        });
        showPlaque(res);
        await loadStats();
        await loadPoints(citySelect.value || undefined);
      } catch (err) {
        say(isNetworkError(err) ? NETWORK_TEXT : errorText(err, "Не получилось отметить — попробуйте ещё раз."), "warn");
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
      const page = await api(`/checkin/search?q=${encodeURIComponent(needle)}`);
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
      return;
    }
    searchResults.append(...items.map(resultRow));
  }

  searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    const value = searchInput.value;
    searchTimer = setTimeout(() => search(value), SEARCH_DEBOUNCE_MS);
  });

  await loadStats();
  await loadPoints();
}
