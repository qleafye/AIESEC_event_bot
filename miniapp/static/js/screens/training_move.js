// Перенос сканов с тренировки: тот же сервис, что кнопка в чате.
// Сессию выбирают кнопкой. Повтор не отмечает человека второй раз.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть перенос. Проверьте интернет и нажмите «Открыть экран снова».";

export async function render(root, params, ctx) {
  const { h, api } = ctx;

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Перенос сканов" }),
      h("p", { class: "thin-screen-state", text: text || FAIL }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Открыть экран снова",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const paint = (data) => {
    const people = Array.isArray(data && data.people) ? data.people : [];
    const sessions = Array.isArray(data && data.sessions) ? data.sessions : [];
    const nodes = [
      h("h1", { text: "Перенос сканов" }),
      h("p", { text: typeof data.text === "string" ? data.text : "" }),
    ];
    for (const person of people) {
      if (!person || !person.name) continue;
      nodes.push(h("p", { class: "label-role", text: person.name }));
    }
    if (!data.pick_city) {
      for (const session of sessions) {
        if (!session || !session.id || !session.label) continue;
        nodes.push(h("button", {
          type: "button",
          class: "btn secondary",
          text: session.label,
          onClick: () => ask(session.id),
        }));
      }
    }
    root.replaceChildren(...nodes);
  };

  const ask = async (sessionId) => {
    let data;
    try {
      data = await api("/forum-day/training-move", {
        method: "POST",
        body: { session_id: sessionId, confirm: false },
      });
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
      return;
    }
    if (!data || data.ok === false || !data.need_confirm) {
      fail((data && data.text) || FAIL);
      return;
    }
    root.replaceChildren(
      h("h1", { text: "Перенос сканов" }),
      h("p", { text: data.text }),
      h("button", {
        type: "button",
        class: "btn",
        text: "Отметить",
        onClick: () => send(sessionId),
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Назад к списку",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const send = async (sessionId) => {
    let data;
    try {
      data = await api("/forum-day/training-move", {
        method: "POST",
        body: { session_id: sessionId, confirm: true },
      });
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
      return;
    }
    if (!data || data.ok === false) {
      fail((data && data.text) || FAIL);
      return;
    }
    root.replaceChildren(
      h("h1", { text: "Перенос сканов" }),
      h("p", { text: data.text || "Готово." }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "К списку",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  root.replaceChildren(h("h1", { text: "Перенос сканов" }), h("p", { text: "Открываем список…" }));
  let data;
  try {
    data = await api("/forum-day/training-move");
  } catch (err) {
    if (isCoreHandledError(err)) return;
    fail();
    return;
  }
  if (!data || typeof data !== "object") {
    fail();
    return;
  }
  paint(data);
}
