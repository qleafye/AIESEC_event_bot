// Письмо одному человеку: тот же допуск, что кнопка «Написать» на карточке.
// Фото и файл остаются в чате. Здесь уходит только текст, и это не рассылка.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть письмо. Проверьте интернет и нажмите «Открыть экран снова».";

export async function render(root, params, ctx) {
  const { h, api } = ctx;
  let picked = null;

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Написать" }),
      h("p", { class: "thin-screen-state", text: text || FAIL }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Открыть экран снова",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const searchBox = h("input", {
    class: "input",
    type: "text",
    placeholder: "Имя",
  });

  const paintSearch = (data) => {
    const people = Array.isArray(data && data.people) ? data.people : [];
    const nodes = [
      h("h1", { text: "Написать" }),
      h("p", { text: typeof data.text === "string" ? data.text : "" }),
    ];
    if (data && data.file_note) nodes.push(h("p", { class: "label-role", text: data.file_note }));
    nodes.push(searchBox);
    nodes.push(h("button", {
      type: "button",
      class: "btn",
      text: "Найти",
      onClick: () => lookup(searchBox.value || ""),
    }));
    for (const person of people) {
      if (!person || !person.id || !person.name) continue;
      const city = person.city ? `, ${person.city}` : "";
      nodes.push(h("button", {
        type: "button",
        class: "btn secondary",
        text: person.name + city,
        onClick: () => compose(person),
      }));
    }
    root.replaceChildren(...nodes);
  };

  const lookup = async (query) => {
    let data;
    try {
      data = await api(`/forum-day/letter?q=${encodeURIComponent(query)}`);
    } catch (err) {
      if (isCoreHandledError(err)) return;
      fail();
      return;
    }
    if (!data || typeof data !== "object") {
      fail();
      return;
    }
    paintSearch(data);
  };

  const compose = (person) => {
    picked = person;
    const area = h("textarea", { class: "input", rows: "4" });
    root.replaceChildren(
      h("h1", { text: "Написать" }),
      h("p", { text: person.name }),
      area,
      h("button", {
        type: "button",
        class: "btn",
        text: "Дальше",
        onClick: () => ask(person.id, area.value || ""),
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "К поиску",
        onClick: () => lookup(searchBox.value || ""),
      }),
    );
  };

  const ask = async (tid, text) => {
    let data;
    try {
      data = await api("/forum-day/letter", {
        method: "POST",
        body: { telegram_id: tid, text, confirm: false },
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
      h("h1", { text: "Написать" }),
      h("p", { text: data.text }),
      h("button", {
        type: "button",
        class: "btn",
        text: "Отправить",
        onClick: () => send(tid, text),
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Назад",
        onClick: () => picked && compose(picked),
      }),
    );
  };

  const send = async (tid, text) => {
    let data;
    try {
      data = await api("/forum-day/letter", {
        method: "POST",
        body: { telegram_id: tid, text, confirm: true },
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
      h("h1", { text: "Написать" }),
      h("p", { text: data.text || "Письмо поставлено в очередь." }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "К поиску",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  await lookup("");
}
