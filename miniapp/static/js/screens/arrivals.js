// Тонкое чтение отметок площадки. Журнал, рассылка и CSV остаются в чате.
// GET /checkin/floor: present, arrived, approved; sessions — названия, без кнопок.

import { isCoreHandledError } from "../ui.js";

export async function render(root, params, ctx) {
  const { h, api } = ctx;
  const count = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.trunc(n) : 0;
  };
  const figure = (value, label) => h("div", { class: "stat" },
    h("div", { class: "home-figure", text: String(value) }),
    h("div", { class: "label-role", text: label }),
  );
  const fail = () => {
    root.replaceChildren(
      h("h1", { text: "Отметки" }),
      h("p", {
        class: "thin-screen-state",
        text: "Не удалось открыть экран. Проверьте интернет и нажмите «Открыть экран снова».",
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Открыть экран снова",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const body = h("div");
  root.replaceChildren(h("h1", { text: "Отметки" }), body);
  body.replaceChildren(
    figure("…", "Сейчас на площадке"),
    figure("…", "Пришли"),
    figure("…", "Одобрено"),
  );

  let data;
  try {
    data = await api("/checkin/floor");
  } catch (err) {
    if (isCoreHandledError(err)) return;
    fail();
    return;
  }
  if (!data || typeof data !== "object") {
    fail();
    return;
  }

  const present = count(data.present);
  const arrived = count(data.arrived);
  const approved = count(data.approved);
  const nodes = [
    figure(present, "Сейчас на площадке"),
    figure(arrived, "Пришли"),
    figure(approved, "Одобрено"),
  ];
  if (present === 0 && arrived === 0 && approved === 0) {
    nodes.push(h("p", {
      class: "label-role",
      text: "Пока никто не отмечен. Числа появятся, когда сканер отметит вход.",
    }));
  }
  const raw = Array.isArray(data.sessions) ? data.sessions : [];
  for (const item of raw) {
    const title = typeof item === "string" ? item : (item && typeof item.title === "string" ? item.title : "");
    if (!title) continue;
    if (item && typeof item === "object" && item.live === false) continue;
    nodes.push(h("p", { class: "label-role", text: title }));
  }
  body.replaceChildren(...nodes);
}
