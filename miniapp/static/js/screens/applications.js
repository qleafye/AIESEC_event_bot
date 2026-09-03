// Экран #/applications — отбор заявок «тиндером» (Phase 23, APP-TINDER-03, D-01..D-04/D-09).
// Форма зеркалит screens/review.js (очередь-«тиндер» геймификации — соседняя, её не трогаем):
// render(root, params, ctx), локальные offset/busy, say(text, kind), «переходим дальше» на
// 404/«already», 401/403/503 не перехватываются (ядро само рисует экран состояния).
//
// Отличие от review.js — продиктовано D-04: решение принимается СВАЙПОМ (swipe.js::attachSwipe)
// ИЛИ парой больших кнопок — жест не подменяет кнопку, а дублирует её же действие. Шторка
// причины отказа, тост отмены, фильтры и «Принять всех N» — задача 3 этого плана (D-05..D-08).
//
// D-25 (0 хардкода): каждая надпись экрана — из ответа `/applications/next` (карточка) или из
// `body.dataset.applicationsTexts` (статичные подписи кнопок — план 23-05 нашёл, что 23-04 их
// не вернул нигде, дописано в miniapp/routers/page.py::APPLICATIONS_TEXT_KEYS, тот же приём,
// что hub.js::sectionLabelsFromDom). Исключение — aria-label без ключа в реестре: берёт
// ближайший по смыслу серверный текст, а не литерал (см. resumeNode/decide-кнопки ниже).

import { icon } from "../icons.js";
import { emptyState, flatRow } from "../ui.js";
import { haptic } from "../motion.js";
import { attachSwipe } from "../swipe.js";

const EDITED_BADGE_KINDS = ["edited", "resubmit"];

// unmount() — отдельная функция вне render(), поэтому то, что она обязана снять (жест),
// живёт на уровне модуля, а не в замыкании render().
let _detachSwipe = null;

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
  let busy = false;
  let currentCard = null;

  const notice = h("p", { class: "chip warn hidden" });
  const cardHolder = h("div", { class: "appl-card-holder" });

  root.append(h("h1", { text: sectionLabel("applications") }), notice, cardHolder);

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "warn"}${text ? "" : " hidden"}`;
  }

  // ── решение по одной заявке (D-04) — свайп и обе кнопки зовут одно и то же ─────────────
  async function decide(action) {
    if (busy || !currentCard) return;
    busy = true;
    const tid = currentCard.application.telegram_id;
    try {
      // Два литеральных пути (не `${action}`) — тот же приём, что review.js::decide.
      const res = action === "approve"
        ? await api(`/applications/${encodeURIComponent(tid)}/approve`, { method: "POST", body: {} })
        : await api(`/applications/${encodeURIComponent(tid)}/reject`, { method: "POST", body: {} });
      if (res.ok) haptic("success");
      // {ok:false, reason:"already"} — кто-то опередил, спокойно едем дальше — тот же load().
      await load();
    } catch (err) {
      if (err && (err.status === 400 || err.status === 403) && err.payload && err.payload.text) {
        say(err.payload.text, "warn");
      } else if (err && err.status === 404) {
        await load();
      }
      // 401/403/503 без текста / прочие — ядро само рисует экран состояния, здесь ничего не делаем.
    } finally {
      busy = false;
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

  function historyNode(history) {
    if (!history || !history.length) return null;
    return h("details", { class: "appl-history" },
      h("summary", {}, icon("history"), h("span", { text: texts.history_label || "" })),
      h("div", { class: "appl-history-list" }, history.map((row) => h("p", { class: "faint", text: row.changed_at || "" }))),
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
      onClick: () => decide("reject"),
    }, icon("x"));

    cardHolder.replaceChildren(
      h("div", { class: "appl-stack", "aria-hidden": "true" }),
      cardEl,
      h("div", { class: "appl-decide-row" }, rejectBtn, approveBtn),
    );

    if (_detachSwipe) { _detachSwipe(); _detachSwipe = null; }
    _detachSwipe = attachSwipe(cardEl, {
      onProgress: (decision, raw) => applyCardTransform(cardEl, overlayApprove, overlayReject, decision, raw),
      onCommit: (action) => {
        resetCardTransform(cardEl, overlayApprove, overlayReject);
        decide(action);
      },
      onCancel: () => resetCardTransform(cardEl, overlayApprove, overlayReject),
    });
  }

  async function load() {
    setMainButton(null);
    cardHolder.classList.add("is-loading");
    let card;
    try {
      card = await api(`/applications/next?offset=${encodeURIComponent(offset)}`);
    } catch (err) {
      cardHolder.classList.remove("is-loading");
      if (err && err.payload && err.payload.text) {
        cardHolder.replaceChildren(h("p", { class: "error-inline", text: err.payload.text }));
      }
      return;
    }
    cardHolder.classList.remove("is-loading");
    if (card.empty) {
      currentCard = null;
      if (_detachSwipe) { _detachSwipe(); _detachSwipe = null; }
      cardHolder.replaceChildren(emptyState(h, { me, text: card.empty_text || "" }));
      return;
    }
    draw(card);
  }

  await load();
}

export function unmount() {
  if (_detachSwipe) { _detachSwipe(); _detachSwipe = null; }
}
