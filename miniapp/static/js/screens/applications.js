// Экран #/applications — отбор заявок «тиндером» (Phase 23, APP-TINDER-03/04, D-01..D-09).
// Форма зеркалит screens/review.js (очередь-«тиндер» геймификации — соседняя, её не трогаем):
// render(root, params, ctx), локальные offset/busy, say(text, kind), «переходим дальше» на
// 404/«already», 401/403/503 не перехватываются (ядро само рисует экран состояния).
//
// Отличия от review.js — продиктованы D-04..D-08:
// 1. Решение принимается СВАЙПОМ (swipe.js::attachSwipe) ИЛИ парой больших кнопок — жест не
//    подменяет кнопку, а дублирует её же действие; свайп влево ТОЛЬКО открывает шторку причины
//    (T-23-23) — решение уходит лишь после выбора шаблона/своей причины/«без причины».
// 2. После решения — тост «Отменить» на undo_seconds ИЗ ОТВЕТА сервера (не константа JS).
// 3. Фильтры-чипы (трек/«изменённые») и «Принять всех N» — своя шапка над карточкой.
//
// D-25 (0 хардкода): каждая надпись экрана — из ответа `/applications/next` (карточка/фильтры)
// или из `body.dataset.applicationsTexts` (статичные подписи кнопок/тостов — план 23-05 нашёл,
// что 23-04 их не вернул нигде, дописано в miniapp/routers/page.py::APPLICATIONS_TEXT_KEYS,
// тот же приём, что hub.js::sectionLabelsFromDom). Исключение — aria-label/alt без ключа в
// реестре: они берут ближайший по смыслу серверный текст, а не литерал (см. resumeNode/
// decide-кнопки ниже).

import { icon } from "../icons.js";
import { emptyState, flatRow, labelText } from "../ui.js";
import { haptic } from "../motion.js";
import { confirmBox, errorText } from "../form.js";
import { attachSwipe } from "../swipe.js";

// Порядок трек-чипов после «Все» (D-08) — коды совпадают с services.applications.TRACK_FILTERS.
const TRACK_CHIP_ORDER = [["full", "full"], ["party", "party"], ["short", "short"]];
const EDITED_BADGE_KINDS = ["edited", "resubmit"];

// unmount() — отдельная функция вне render(), поэтому то, что она обязана снять (жест,
// таймер тоста), живёт на уровне модуля, а не в замыкании render().
let _detachSwipe = null;
let _toastTimer = null;

function applicationsTexts() {
  try {
    return JSON.parse(document.body.dataset.applicationsTexts || "{}");
  } catch (_) {
    return {};
  }
}

function sectionLabel(section) {
  try {
    const labels = JSON.parse(document.body.dataset.sectionLabels || "{}");
    return labels[section] || "";
  } catch (_) {
    return "";
  }
}

export async function render(root, params, ctx) {
  const { h, api, setMainButton, me } = ctx;
  const texts = applicationsTexts();

  let offset = 0;
  let track = null; // null | "full" | "party" | "short"
  let changedOnly = false;
  let busy = false;
  let currentCard = null;
  let filtersData = null; // последний непустой filters — чипы/шаблоны переживают пустую страницу

  const notice = h("p", { class: "chip warn hidden" });
  const filtersRow = h("div", { class: "appl-filters" });
  const approveAllBtn = h("button", { class: "btn ghost hidden", type: "button", onClick: () => openApproveAllConfirm() });
  const cardHolder = h("div", { class: "appl-card-holder" });
  const toast = h("div", { class: "appl-toast hidden" });

  const approveAllConfirm = confirmBox(h, {
    cancelText: texts.undo_button || "",
    onConfirm: () => { approveAllConfirm.close(); approveAll(); },
  });
  // D-07 (план 23-06 закрыл Known Stub 23-05): «Принять всех N» называет и число, и город —
  // `city_label` уже собранный сервером текст (miniapp/routers/applications.py), отдельной
  // строкой под count-текстом, как appr_all_confirm бота. Модуль городов выключен -> сервер
  // отдаёт `null`, строка остаётся скрытой.
  const approveAllCityLine = h("p", { class: "faint hidden" });
  approveAllConfirm.insertBefore(approveAllCityLine, approveAllConfirm.querySelector(".btn.danger"));

  const rejectReasonInput = h("textarea", { class: "input", rows: "2", maxlength: "500" });
  const rejectOwnSubmit = h("button", {
    class: "btn danger", type: "button", text: texts.reject_button || "",
    onClick: () => submitReject(rejectReasonInput.value.trim()),
  });
  const rejectOwnBox = h("div", { class: "field hidden" }, rejectReasonInput, rejectOwnSubmit);
  const rejectOwnToggle = h("button", {
    class: "btn ghost", type: "button", text: texts.reject_own_reason || "",
    onClick: () => rejectOwnBox.classList.toggle("hidden"),
  });
  const rejectTemplatesBox = h("div", { class: "appl-sheet-templates" });
  const rejectNoReasonBtn = h("button", {
    class: "btn ghost", type: "button", text: texts.reject_no_reason || "",
    onClick: () => submitReject(""),
  });
  const rejectSheet = h("div", { class: "appl-sheet hidden" },
    rejectTemplatesBox, rejectOwnToggle, rejectOwnBox, rejectNoReasonBtn,
  );

  root.append(
    // D-04: заголовок экрана без иконки рядом — labelText (ui.js) снимает ведущий эмодзи
    // подписи раздела из реестра (без этого <h1> дублирует иконку раздела глифом «🗂»).
    h("h1", { text: labelText(sectionLabel("applications")) }),
    notice,
    h("div", { class: "appl-head" }, filtersRow, approveAllBtn),
    approveAllConfirm,
    cardHolder,
    rejectSheet,
    toast,
  );

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "warn"}${text ? "" : " hidden"}`;
  }

  // ── тост отмены (D-06): один шаг, живёт undo_seconds из ответа сервера ──────────────────
  function hideToast() {
    if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
    toast.classList.add("hidden");
    toast.replaceChildren();
  }

  function showToast(text, seconds, onUndo) {
    hideToast();
    toast.replaceChildren(
      h("span", { text: text || "" }),
      h("button", {
        class: "btn ghost", type: "button", text: texts.undo_button || "",
        onClick: () => { hideToast(); onUndo(); },
      }),
    );
    toast.classList.remove("hidden");
    if (seconds > 0) _toastTimer = setTimeout(hideToast, seconds * 1000);
  }

  // ── фильтры-чипы (D-08) ───────────────────────────────────────────────────────────────
  function renderFilters() {
    filtersRow.replaceChildren();
    const chips = filtersData && filtersData.chips;
    if (!chips) return;
    function chipBtn(label, active, onClick) {
      return h("button", {
        class: `chip appl-filter-chip${active ? " on" : ""}`, type: "button",
        "aria-pressed": active ? "true" : "false", text: label, onClick,
      });
    }
    if (chips.all) {
      filtersRow.append(chipBtn(chips.all, track === null, () => { if (track !== null) { track = null; offset = 0; load(); } }));
    }
    for (const [key, value] of TRACK_CHIP_ORDER) {
      if (!chips[key]) continue;
      filtersRow.append(chipBtn(chips[key], track === value, () => { if (track !== value) { track = value; offset = 0; load(); } }));
    }
    if (chips.changed) {
      filtersRow.append(chipBtn(chips.changed, changedOnly, () => { changedOnly = !changedOnly; offset = 0; load(); }));
    }
  }

  function renderRejectTemplates() {
    rejectTemplatesBox.replaceChildren();
    const templates = (filtersData && filtersData.reject_templates) || [];
    for (const tpl of templates) {
      rejectTemplatesBox.append(h("button", { class: "btn ghost", type: "button", text: tpl, onClick: () => submitReject(tpl) }));
    }
  }

  function updateHeader(remaining) {
    if (remaining > 0 && texts.approve_all_button) {
      approveAllBtn.classList.remove("hidden");
      approveAllBtn.textContent = texts.approve_all_button.replace("{count}", String(remaining));
    } else {
      approveAllBtn.classList.add("hidden");
    }
  }

  // ── шторка отказа (D-05/T-23-23): свайп влево и кнопка «❌» её только ОТКРЫВАЮТ ─────────
  function openRejectSheet() {
    if (!currentCard) return;
    rejectReasonInput.value = "";
    rejectOwnBox.classList.add("hidden");
    rejectSheet.classList.remove("hidden");
  }

  function closeRejectSheet() {
    rejectSheet.classList.add("hidden");
  }

  async function submitReject(reason) {
    closeRejectSheet();
    await decide("reject", reason);
  }

  // ── «Принять всех N» (D-07) ──────────────────────────────────────────────────────────
  function openApproveAllConfirm() {
    if (busy || !currentCard || !currentCard.remaining) return;
    const count = currentCard.remaining;
    approveAllConfirm.querySelector("p").textContent = (texts.approve_all_confirm || "").replace("{count}", String(count));
    const cityLabel = currentCard.city_label || "";
    approveAllCityLine.textContent = cityLabel;
    approveAllCityLine.classList.toggle("hidden", !cityLabel);
    const confirmBtn = approveAllConfirm.querySelector(".btn.danger");
    if (confirmBtn) confirmBtn.textContent = (texts.approve_all_button || "").replace("{count}", String(count));
    approveAllConfirm.open();
  }

  async function approveAll() {
    if (busy) return;
    busy = true;
    try {
      // Город менеджера в теле запроса: реальная привязка ("spb") или маркер "*" (все города,
      // сверяется сервером как ALL_CITIES) — данные протокола, не текст человеку.
      const city = me && me.city != null ? me.city : "*";
      const res = await api("/applications/approve_all", { method: "POST", body: { city } });
      if (res.ok) {
        say("", null);
        offset = 0;
      }
      // {ok:false, reason:"already"} — кто-то опередил между диалогом и подтверждением, не
      // ошибка (T-23-26 логика): просто перечитываем очередь тем же load() ниже.
      await load();
    } catch (err) {
      const msg = errorText(err, "");
      if (msg) say(msg, "warn");
    } finally {
      busy = false;
    }
  }

  // ── решение по одной заявке (D-04/D-05/D-06) ────────────────────────────────────────
  async function decide(action, reason) {
    if (busy || !currentCard) return;
    busy = true;
    const tid = currentCard.application.telegram_id;
    try {
      // Два литеральных пути (не `${action}`) — тот же приём, что review.js::decide.
      const res = action === "approve"
        ? await api(`/applications/${encodeURIComponent(tid)}/approve`, { method: "POST", body: {} })
        : await api(`/applications/${encodeURIComponent(tid)}/reject`, { method: "POST", body: { reason: reason || "" } });
      if (res.ok) {
        haptic("success");
        const toastText = action === "approve" ? texts.approved_toast : texts.rejected_toast;
        showToast(toastText, res.undo_seconds, () => undoDecision(res.decision_id));
        await load();
      } else {
        // {ok:false, reason:"already"} — кто-то опередил, спокойно едем дальше.
        await load();
      }
    } catch (err) {
      if (err && (err.status === 400 || err.status === 403) && err.payload && err.payload.text) {
        say(err.payload.text, "warn");
      } else if (err && err.status === 404) {
        await load();
      } else if (!(err && (err.status === 401 || err.status === 403 || err.status === 503))) {
        const msg = errorText(err, "");
        if (msg) say(msg, "warn");
      }
    } finally {
      busy = false;
    }
  }

  async function undoDecision(decisionId) {
    if (busy) return;
    busy = true;
    try {
      const res = await api("/applications/undo", { method: "POST", body: { decision_id: decisionId } });
      say(res.ok ? (texts.undone_toast || "") : (texts.undo_too_late || ""), res.ok ? "success" : "accent");
    } catch (err) {
      const msg = errorText(err, "");
      if (msg) say(msg, "warn");
    } finally {
      busy = false;
      offset = 0;
      await load();
    }
  }

  // ── карточка (D-01/D-02/D-03) ────────────────────────────────────────────────────────
  function initialsNode(initials) {
    return h("div", { class: "appl-avatar-initials", text: initials || "" });
  }

  function avatarNode(avatar) {
    if (!avatar.url) return initialsNode(avatar.initials);
    const img = h("img", { class: "appl-avatar", alt: "", src: avatar.url });
    img.addEventListener("error", () => img.replaceWith(initialsNode(avatar.initials)));
    return img;
  }

  function resumeNode(resume) {
    if (resume.kind === "file") {
      return h("a", { class: "btn ghost appl-resume-open", href: resume.url, target: "_blank", rel: "noopener" },
        icon("file-text"), h("span", { text: texts.resume_open || "" }));
    }
    if (resume.kind === "text") {
      return h("blockquote", { class: "appl-resume-quote pre", text: resume.text || "" });
    }
    return h("p", { class: "muted", text: texts.resume_none || "" });
  }

  // «Было → стало» — план 23-06 закрыл Known Stub 23-05: `row.when`/`row.source_label`/
  // `row.changes[].label` уже переведены сервером (services.applications._history_entry),
  // здесь нет ни одного кода колонки/источника — только готовые подписи.
  function historyChangeNode(change) {
    return h("p", { class: "appl-history-change" },
      h("span", { class: "appl-history-label", text: change.label || "" }),
      h("span", { class: "appl-history-values" },
        h("span", { text: change.old != null ? String(change.old) : "" }),
        icon("chevron-right", { class: "appl-history-arrow" }),
        h("span", { text: change.new != null ? String(change.new) : "" }),
      ),
    );
  }

  function historyEntryNode(entry) {
    const metaText = [entry.when, entry.source_label].filter(Boolean).join(" · ");
    return h("div", { class: "appl-history-entry" },
      h("p", { class: "faint", text: metaText }),
      (entry.changes || []).map(historyChangeNode),
    );
  }

  function historyNode(history) {
    if (!history || !history.length) return null;
    return h("details", { class: "appl-history" },
      h("summary", {}, icon("history"), h("span", { text: texts.history_label || "" })),
      h("div", { class: "appl-history-list" }, history.map(historyEntryNode)),
    );
  }

  function applyCardTransform(el, overlayApprove, overlayReject, decision, raw) {
    if (decision.vertical || decision.edge) {
      el.style.transform = "";
      overlayApprove.style.opacity = "0";
      overlayReject.style.opacity = "0";
      return;
    }
    const progress = decision.progress || 0;
    el.style.transform = progress ? `translateX(${raw.dx}px) rotate(${decision.tilt || 0}deg)` : "";
    overlayApprove.style.opacity = raw.dx > 0 ? String(progress) : "0";
    overlayReject.style.opacity = raw.dx < 0 ? String(progress) : "0";
  }

  function resetCardTransform(el, overlayApprove, overlayReject) {
    el.style.transform = "";
    overlayApprove.style.opacity = "0";
    overlayReject.style.opacity = "0";
  }

  function draw(card) {
    currentCard = card;
    filtersData = card.filters || filtersData;
    renderFilters();
    renderRejectTemplates();
    updateHeader(card.remaining);

    const app = card.application;
    const metaText = [app.username ? `@${app.username}` : null, app.city].filter(Boolean).join(" · ");
    const badgeNodes = (card.badges || []).map((b) => h("span", {
      class: `chip ${EDITED_BADGE_KINDS.includes(b.kind) ? "accent" : ""}`.trim(),
      text: b.text,
    }));
    const mainFieldNodes = (card.main_fields || []).map((f) => flatRow(h, { title: f.label, meta: f.value }));
    const extraFields = card.extra_fields || [];
    const extraBlock = h("div", { class: "flat-list appl-extra hidden" }, extraFields.map((f) => flatRow(h, { title: f.label, meta: f.value })));
    const showAllBtn = extraFields.length
      ? h("button", {
          class: "btn ghost appl-show-all", type: "button", "aria-expanded": "false",
          onClick: () => {
            const nowHidden = extraBlock.classList.toggle("hidden");
            showAllBtn.setAttribute("aria-expanded", nowHidden ? "false" : "true");
          },
        }, icon("chevron-down"), h("span", { text: texts.show_all || "" }))
      : null;

    const overlayApprove = h("div", { class: "appl-overlay approve", "aria-hidden": "true" }, icon("check"));
    const overlayReject = h("div", { class: "appl-overlay reject", "aria-hidden": "true" }, icon("x"));

    const cardEl = h("article", { class: "card appl-card" },
      h("div", { class: "appl-head-row" },
        avatarNode(card.avatar),
        h("div", { class: "appl-name-block" },
          h("div", { class: "flat-row-title", text: app.full_name || "" }),
          h("div", { class: "flat-row-meta", text: metaText }),
        ),
        h("div", { class: "faint appl-position", text: `${card.position} / ${card.remaining}` }),
      ),
      badgeNodes.length ? h("div", { class: "appl-badges" }, badgeNodes) : null,
      h("div", { class: "flat-list appl-fields" }, mainFieldNodes),
      showAllBtn, extraBlock,
      h("div", { class: "appl-resume" }, resumeNode(card.resume)),
      historyNode(card.history),
      overlayApprove, overlayReject,
    );

    const approveBtn = h("button", {
      class: "btn appl-decide approve", type: "button", "aria-label": texts.approve_button || "",
      onClick: () => decide("approve"),
    }, icon("check"));
    const rejectBtn = h("button", {
      class: "btn ghost appl-decide reject", type: "button", "aria-label": texts.reject_button || "",
      onClick: () => openRejectSheet(),
    }, icon("x"));

    // D-04 (фикс «подложка закрывает кнопки», квик 03.09): стопка (тень следующей карточки +
    // сама карточка) — отдельная обёртка `.appl-stack-wrap`, а не прямой ребёнок cardHolder.
    // Абсолютно спозиционированная `.appl-stack` (z-index:0) в CSS-каскаде красится ПОВЕРХ
    // статичного in-flow контента (правило CSS2.1 stacking order — позиционированные потомки
    // со stack level 0 красятся над не-позиционированными block-потомками независимо от
    // порядка в DOM), поэтому ряд кнопок решения обязан быть СОСЕДОМ обёртки стопки, а не
    // третьим ребёнком того же контейнера, где стопка технически перекрывает его геометрию.
    cardHolder.replaceChildren(
      h("div", { class: "appl-stack-wrap" },
        h("div", { class: "appl-stack", "aria-hidden": "true" }),
        cardEl,
      ),
      h("div", { class: "appl-decide-row" }, rejectBtn, approveBtn),
    );

    if (_detachSwipe) { _detachSwipe(); _detachSwipe = null; }
    _detachSwipe = attachSwipe(cardEl, {
      onProgress: (decision, raw) => applyCardTransform(cardEl, overlayApprove, overlayReject, decision, raw),
      onCommit: (action) => {
        resetCardTransform(cardEl, overlayApprove, overlayReject);
        if (action === "approve") decide("approve");
        else openRejectSheet();
      },
      onCancel: () => resetCardTransform(cardEl, overlayApprove, overlayReject),
    });
  }

  async function load() {
    setMainButton(null);
    closeRejectSheet();
    cardHolder.classList.add("is-loading");
    let card;
    try {
      const query = new URLSearchParams({ offset: String(offset) });
      if (track) query.set("track", track);
      if (changedOnly) query.set("changed", "1");
      card = await api(`/applications/next?${query.toString()}`);
    } catch (err) {
      cardHolder.classList.remove("is-loading");
      const msg = errorText(err, "");
      if (msg) cardHolder.replaceChildren(h("p", { class: "error-inline", text: msg }));
      return;
    }
    cardHolder.classList.remove("is-loading");
    if (card.empty) {
      currentCard = null;
      if (_detachSwipe) { _detachSwipe(); _detachSwipe = null; }
      updateHeader(card.remaining);
      renderFilters();
      renderRejectTemplates();
      cardHolder.replaceChildren(emptyState(h, { me, text: card.empty_text || "" }));
      return;
    }
    draw(card);
  }

  await load();
}

export function unmount() {
  // Тост/побочные эффекты решения сервер довезёт сам (D-06) — здесь только визуальная уборка.
  if (_detachSwipe) { _detachSwipe(); _detachSwipe = null; }
  if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
}
