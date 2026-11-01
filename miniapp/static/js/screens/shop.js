// Экран #/shop — «🛍 Магазин» делегата: витрина магазина баллов с фото, ценой и остатком,
// «Взять» (бронь, баллы списываются сразу) и «Мои брони» с кодом выдачи, QR и отменой.
// Всё решает сервер (`GET /app/api/shop`, miniapp/routers/shop.py — тот же слой брони, что у
// кнопки «Взять» в чате): статус товара (`ok`/`sold_out`/`limit`/`short`), пометку `note`,
// можно ли взять (`can_take`), текст подтверждения. Клиент только рисует готовый ответ и
// спрашивает подтверждение перед списанием и отменой. Подписи — полем `texts` уже переведёнными,
// 0 литералов. Двойной тап «Да, списать»: кнопка блокируется на время запроса, а `request_id`,
// сгенерированный при открытии подтверждения, сервер узнаёт и отдаёт ту же бронь.

import { emptyState, errorText, fileUrl, guardedRender, labelText, noticeBox, screenText, sectionTitle } from "../ui.js";
import { haptic } from "../motion.js";

function sectionLabel(section) {
  try {
    const labels = JSON.parse(document.body.dataset.sectionLabels || "{}");
    return labels[section] || "";
  } catch (_) {
    return "";
  }
}

function newRequestId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  } catch (_) {
    // нет crypto.randomUUID (старый вебвью) — запасной вариант ниже
  }
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`.slice(0, 64).padEnd(12, "0");
}

async function draw(root, params, ctx) {
  const { h, api, me } = ctx;
  const notice = noticeBox(h, { autoHideMs: 6000 });
  root.append(h("h1", { text: labelText(sectionLabel("shop")) }));

  let page;
  try {
    page = await api("/shop");
  } catch (err) {
    // Витрину скрыли, пока экран был открыт, — это не «Нет доступа», а пустое состояние.
    if (err && err.status === 404 && err.reason === "shop_closed") {
      root.append(emptyState(h, { me, text: errorText(err, "") }));
      return;
    }
    throw err;
  }
  const texts = page.texts || {};
  const items = page.items || [];
  const reservations = page.reservations || [];
  // Одно открытое подтверждение на экране: новое закрывает прежнее.
  let openBox = null;
  let busy = false;

  function closeBox() {
    if (openBox) openBox.remove();
    openBox = null;
  }

  async function redraw(text, kind) {
    await guardedRender(root, ctx, () => draw(root, params, ctx));
    if (text) {
      const fresh = noticeBox(h, { autoHideMs: 6000 });
      root.prepend(fresh.el);
      fresh.say(text, kind);
    }
  }

  async function act(button, call) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    try {
      const res = await call();
      haptic("success");
      await redraw(res.text || "", "success");
    } catch (err) {
      if (err && err.status === 409) {
        await redraw(errorText(err, ""), "warn");
      } else {
        notice.say(errorText(err, screenText("load_error")), "warn");
      }
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  function confirmBox(holder, text, yesLabel, noLabel, onYes) {
    closeBox();
    const yes = h("button", { class: "btn", type: "button", text: yesLabel });
    yes.addEventListener("click", () => onYes(yes));
    const box = h("div", { class: "confirm-box" },
      h("p", { class: "shop-confirm-text", text }),
      yes,
      h("button", { class: "btn ghost", type: "button", text: noLabel, onClick: closeBox }),
    );
    holder.append(box);
    openBox = box;
  }

  // ── плита: название и баланс ──
  root.append(
    h("section", { class: "plate plate--list" },
      h("div", { class: "plate-eyebrow", text: page.title || "" }),
      h("div", { class: "plate-title", text: page.balance_text || "" }),
      h("p", { class: "shop-hint", text: page.reserve_on ? texts.hint : texts.reserve_off }),
    ),
    notice.el,
  );

  // ── мои брони ──
  if (reservations.length) {
    root.append(sectionTitle(h, texts.my_title));
    const list = h("div", { class: "shop-list" });
    for (const r of reservations) {
      const qrSlot = h("div", { class: "shop-qr" });
      const row = h("div", { class: "shop-item shop-reservation" },
        h("div", { class: "shop-item-title", text: r.title }),
        h("div", { class: "shop-item-meta", text: r.code_text }),
      );
      const qrButton = h("button", { class: "btn secondary", type: "button", text: texts.qr_button });
      qrButton.addEventListener("click", async () => {
        if (qrSlot.childElementCount) { qrSlot.replaceChildren(); return; }
        try {
          const res = await api(`/shop/reservations/${r.id}/qr`);
          qrSlot.replaceChildren(h("img", { class: "shop-qr-img", src: res.qr, alt: r.code }));
        } catch (err) {
          if (err && err.status === 404) await redraw(errorText(err, ""), "warn");
          else notice.say(errorText(err, screenText("load_error")), "warn");
        }
      });
      const cancelButton = h("button", { class: "btn ghost", type: "button", text: texts.cancel_button });
      cancelButton.addEventListener("click", () => confirmBox(row, r.cancel_confirm_text, texts.cancel_yes, texts.keep,
        (yes) => act(yes, () => api(`/shop/reservations/${r.id}/cancel`, { method: "POST" }))));
      row.append(h("div", { class: "task-actions" }, qrButton, cancelButton), qrSlot);
      list.append(row);
    }
    root.append(list);
  }

  // ── товары ──
  if (!items.length) {
    root.append(emptyState(h, { me, text: page.empty_text || "" }));
    return;
  }
  const list = h("div", { class: "shop-list" });
  for (const item of items) {
    const card = h("article", { class: "shop-item" });
    if (item.photo_file_id) {
      const img = h("img", { class: "cover", alt: "", loading: "lazy", src: fileUrl(item.photo_file_id) });
      img.addEventListener("error", () => img.remove());
      card.append(img);
    }
    card.append(
      h("div", { class: "shop-item-title", text: item.title }),
      h("div", { class: "shop-item-meta", text: [item.price_text, item.stock_text].filter(Boolean).join(" · ") }),
    );
    if (item.note) card.append(h("div", { class: "shop-item-note muted", text: item.note }));
    if (item.can_take) {
      const take = h("button", { class: "btn", type: "button", text: texts.take });
      take.addEventListener("click", () => {
        const requestId = newRequestId();
        confirmBox(card, item.confirm_text, texts.take_yes, texts.cancel,
          (yes) => act(yes, () => api("/shop/reserve", { method: "POST", body: { item_id: item.id, request_id: requestId } })));
      });
      card.append(h("div", { class: "task-actions" }, take));
    }
    list.append(card);
  }
  root.append(list);
}

export async function render(root, params, ctx) {
  await guardedRender(root, ctx, () => draw(root, params, ctx));
}
