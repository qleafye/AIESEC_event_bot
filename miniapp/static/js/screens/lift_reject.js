// Снять отказ с одной заявки: тот же флип, что кнопка на карточке в чате.
// Список целиком отсюда не одобряется. Решение человеку уходит очередью бота.

import { isCoreHandledError } from "../ui.js";

const FAIL = "Не получилось открыть заявку. Проверьте интернет и нажмите «Открыть экран снова».";

export async function render(root, params, ctx) {
  const { h, api } = ctx;

  const fail = (text) => {
    root.replaceChildren(
      h("h1", { text: "Снять отказ" }),
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

  const paint = (data) => {
    const people = Array.isArray(data && data.people) ? data.people : [];
    const nodes = [
      h("h1", { text: "Снять отказ" }),
      h("p", { text: typeof data.text === "string" ? data.text : "" }),
      searchBox,
      h("button", {
        type: "button",
        class: "btn",
        text: "Найти",
        onClick: () => lookup(searchBox.value || ""),
      }),
    ];
    for (const person of people) {
      if (!person || !person.id || !person.name) continue;
      const city = person.city ? `, ${person.city}` : "";
      nodes.push(h("button", {
        type: "button",
        class: "btn secondary",
        text: person.name + city,
        onClick: () => ask(person.id),
      }));
    }
    root.replaceChildren(...nodes);
  };

  const lookup = async (query) => {
    let data;
    try {
      data = await api(`/forum-day/lift-reject?q=${encodeURIComponent(query)}`);
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
  };

  const ask = async (telegramId) => {
    let data;
    try {
      data = await api("/forum-day/lift-reject", {
        method: "POST",
        body: { telegram_id: telegramId, confirm: false },
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
      h("h1", { text: "Снять отказ" }),
      h("p", { text: data.text }),
      h("button", {
        type: "button",
        class: "btn",
        text: "Снять отказ",
        onClick: () => send(telegramId),
      }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "Назад к поиску",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  const send = async (telegramId) => {
    let data;
    try {
      data = await api("/forum-day/lift-reject", {
        method: "POST",
        body: { telegram_id: telegramId, confirm: true },
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
      h("h1", { text: "Снять отказ" }),
      h("p", { text: data.text || "Готово." }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: "К поиску",
        onClick: () => render(root, params, ctx),
      }),
    );
  };

  paint({ text: "Введите имя — хотя бы две буквы.", people: [] });
}
