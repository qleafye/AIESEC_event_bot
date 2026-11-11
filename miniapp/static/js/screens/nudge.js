// Догонялка несдатчиков: тот же список, что кнопка на карточке анкеты.
// Текст приглашения задаётся в анкете, здесь его не пишут.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть список. Проверьте интернет и нажмите «Открыть экран снова».";

export async function render(root, params, ctx) {
  const { h, api } = ctx;

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Догонялка" }),
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
    const surveys = Array.isArray(data && data.surveys) ? data.surveys : [];
    const nodes = [
      h("h1", { text: "Догонялка" }),
      h("p", { text: typeof data.text === "string" ? data.text : "" }),
    ];
    for (const survey of surveys) {
      if (!survey || !survey.id) continue;
      const title = String(survey.question || "анкета").replace(/\s+/g, " ").trim() || "анкета";
      const count = Number(survey.count) || 0;
      const people = Array.isArray(survey.people) ? survey.people : [];
      if (!count) {
        nodes.push(h("p", { text: `${title} — напоминать некого` }));
        continue;
      }
      nodes.push(h("button", {
        type: "button",
        class: "btn secondary",
        text: `${title} — ${count} чел.`,
        onClick: () => ask(survey.id),
      }));
      for (const person of people) {
        if (!person || !person.name) continue;
        nodes.push(h("p", { class: "label-role", text: person.name }));
      }
    }
    root.replaceChildren(...nodes);
  };

  const ask = async (pollId) => {
    let data;
    try {
      data = await api("/forum-day/nudge", {
        method: "POST",
        body: { poll_id: pollId, confirm: false },
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
      h("h1", { text: "Догонялка" }),
      h("p", { text: data.text }),
      h("button", {
        type: "button",
        class: "btn",
        text: "Напомнить",
        onClick: () => send(pollId),
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Назад к списку",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const send = async (pollId) => {
    let data;
    try {
      data = await api("/forum-day/nudge", {
        method: "POST",
        body: { poll_id: pollId, confirm: true },
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
      h("h1", { text: "Догонялка" }),
      h("p", { text: data.text || "Напоминание поставлено в очередь." }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "К списку",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  root.replaceChildren(h("h1", { text: "Догонялка" }), h("p", { text: "Открываем список…" }));
  let data;
  try {
    data = await api("/forum-day/nudge");
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
