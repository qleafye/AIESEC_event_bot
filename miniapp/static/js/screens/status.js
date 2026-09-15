// Полноэкранный статус заявки (30-UI-SPEC.md § «Экран статуса заявки», план 30-05 задача 3,
// A2-07): три состояния — на проверке / одобрена / отклонена, один и тот же ответ
// `GET /app/api/hub/status`, что уже кормит короткую плиту-ссылку на хабе (`hub.js`) — вторую
// ручку статуса план не заводит (`<interfaces>` 30-05-PLAN.md).
//
// Рисуется ТОЛЬКО когда `status.status_screen_enabled` истинен (сервер сам решает по тумблеру
// `reg_form_status_screen`, `miniapp/routers/hub.py`) — выключенный тумблер уводит делегата на
// хаб, а не рисует пустой экран (сегодняшнее поведение вместо этого экрана — строка статуса
// в профиле, `screens/profile.js`, её этот план не трогает).

import { icon } from "../icons.js";
import { guardedRender } from "../ui.js";
import { confetti } from "../motion.js";

const BADGE_TONE = { pending: "accent", approved: "success", rejected: "danger" };
const BADGE_ICON = { pending: "clock-4", approved: "check", rejected: "alert-triangle" };

function toneOf(status) {
  return BADGE_TONE[status] || "accent";
}

function stepRow(h, step, index) {
  return h("div", { class: "status-step" },
    h("span", { class: "num", text: String(index + 1).padStart(2, "0") }),
    h("div", {},
      h("div", { class: "title", text: step.title || "" }),
      step.body ? h("div", { class: "body", text: step.body }) : null,
    ),
  );
}

function paymentCard(h, status) {
  const p = status.payment;
  if (!p) return null;
  return h("div", { class: "card" },
    p.due_label ? h("div", { class: "title", text: p.due_label }) : null,
    h("div", { class: "cv", text: `${p.amount} ₽` }),
    p.reminder_note ? h("p", { class: "label-role", text: p.reminder_note }) : null,
  );
}

function reasonCard(h, status) {
  if (!status.reason_text) return null;
  const dateNode = status.reason_date ? h("p", { class: "label-role", text: status.reason_date }) : null;
  return h("div", { class: "card" },
    status.reason_eyebrow ? h("div", { class: "plate-eyebrow", text: status.reason_eyebrow }) : null,
    h("p", { text: status.reason_text }),
    dateNode,
  );
}

async function draw(root, params, ctx) {
  const { h, api, navigate, setMainButton } = ctx;
  const status = await api("/hub/status");

  // Выключенный тумблер — сегодняшнее поведение вместо экрана: назад на хаб, никакого
  // «пустого» экрана делегат не увидит (deviation rule 3, checkpoints.md — automation-first).
  if (!status || !status.status_screen_enabled) {
    navigate("#/hub");
    return;
  }

  const tone = toneOf(status.status);
  const badge = h("span", { class: `chip ${tone}` },
    icon(BADGE_ICON[status.status] || "clock-4"),
    h("span", { text: status.badge || "" }),
  );

  const plate = h("section", { class: `plate plate--status status-${tone}` },
    h("div", { class: "plate-row" }, badge),
    h("h1", { class: "status-title", text: status.title || "" }),
    status.screen_body ? h("p", { class: "plate-sub", text: status.screen_body }) : null,
  );

  const sections = [];

  if (status.next_steps && status.next_steps.length) {
    sections.push(h("div", {},
      status.next_steps_eyebrow ? h("p", { class: "label-role", text: status.next_steps_eyebrow }) : null,
      ...status.next_steps.map((step, i) => stepRow(h, step, i)),
    ));
  }

  const payment = paymentCard(h, status);
  if (payment) sections.push(payment);

  const reason = reasonCard(h, status);
  if (reason) sections.push(reason);

  if (status.status === "rejected") {
    sections.push(h("p", { class: "label-role", text: status.saved_answers_label || "" }));
  }

  if (status.event_dates || status.event_place) {
    sections.push(h("div", { class: "screen-anchor" },
      icon("calendar"),
      h("div", {},
        status.event_dates ? h("div", { class: "screen-anchor-title", text: status.event_dates }) : null,
        status.event_place ? h("div", { class: "screen-anchor-sub", text: status.event_place }) : null,
      ),
    ));
  }

  const mainLabel = status.status === "approved"
    ? status.pay_button_text
    : status.status === "rejected"
      ? status.resubmit_button_text
      : status.edit_button_text;
  const mainAction = () => navigate("#/form");
  const mainBtn = mainLabel ? h("button", {
    class: "btn", type: "button", "aria-label": mainLabel, onClick: mainAction,
  }, h("span", { text: mainLabel }), icon("arrow-right")) : null;

  root.append(...[plate, ...sections, mainBtn ? h("div", { class: "task-actions" }, mainBtn) : null].filter(Boolean));
  // Quick 260915-4mw (ANIM-06): залп сам себя снимает и сам гейтит уровень "full" (motion.js
  // проверяет dataset.motion внутри) — на micro/off его не будет, отдельного тумблера у него
  // нет, второй копии детекта уровня здесь не заводим.
  if (status.status === "approved") confetti(plate);
  setMainButton(mainLabel || null, mainAction);
}

export async function render(root, params, ctx) {
  await guardedRender(root, ctx, () => draw(root, params, ctx));
}

export function unmount() {}
