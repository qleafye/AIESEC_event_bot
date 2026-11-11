// «Позвать резерв»: тот же список, что кнопка чата. Файл к письму остаётся в чате.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть список. Проверьте интернет и нажмите «Открыть экран снова».";

export async function render(root, params, ctx) {
  const { h, api } = ctx;
  const selected = new Set();
  let people = [];
  let pickCity = false;

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Позвать резерв" }),
      h("p", { class: "thin-screen-state", text: text || FAIL }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Открыть экран снова",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const paint = () => {
    const nodes = [h("h1", { text: "Позвать резерв" }), h("p", { text: root._lead || "" })];
    if (root._note) nodes.push(h("p", { class: "label-role", text: root._note }));
    if (pickCity || !people.length) {
      root.replaceChildren(...nodes);
      return;
    }
    for (const person of people) {
      const on = selected.has(person.id);
      nodes.push(h("button", {
        type: "button",
        class: "btn secondary",
        text: (on ? "✓ " : "") + person.name,
        onClick: () => {
          if (selected.has(person.id)) selected.delete(person.id);
          else selected.add(person.id);
          paint();
        },
      }));
    }
    nodes.push(h("button", {
      type: "button",
      class: "btn",
      text: "Всех по этому списку",
      onClick: () => ask("all", []),
    }));
    if (selected.size) {
      nodes.push(h("button", {
        type: "button",
        class: "btn",
        text: `Позвать выбранных (${selected.size})`,
        onClick: () => ask("selected", [...selected]),
      }));
    }
    root.replaceChildren(...nodes);
  };

  const ask = async (mode, ids) => {
    let data;
    try {
      data = await api("/forum-day/reserve", {
        method: "POST",
        body: { mode, ids, confirm: false },
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
      h("h1", { text: "Позвать резерв" }),
      h("p", { text: data.text }),
      h("button", {
        type: "button",
        class: "btn",
        text: "Позвать",
        onClick: () => send(mode, ids),
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Назад к списку",
        onClick: paint,
      }),
    );
  };

  const send = async (mode, ids) => {
    let data;
    try {
      data = await api("/forum-day/reserve", {
        method: "POST",
        body: { mode, ids, confirm: true },
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
    selected.clear();
    root.replaceChildren(
      h("h1", { text: "Позвать резерв" }),
      h("p", { text: data.text || "Письмо поставлено в очередь." }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "К списку",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  root.replaceChildren(h("h1", { text: "Позвать резерв" }), h("p", { text: "Открываем список…" }));
  let data;
  try {
    data = await api("/forum-day/reserve");
  } catch (err) {
    if (isCoreHandledError(err)) return;
    fail();
    return;
  }
  if (!data || typeof data !== "object") {
    fail();
    return;
  }
  pickCity = Boolean(data.pick_city);
  people = Array.isArray(data.people) ? data.people.filter((row) => row && row.id) : [];
  root._lead = typeof data.text === "string" ? data.text : "";
  root._note = typeof data.file_note === "string" ? data.file_note : "";
  paint();
}
