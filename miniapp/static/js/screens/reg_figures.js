// Тонкое чтение цифр регистраций. Не экран геймы и не супердашборд.
// GET /stats/registrations: total, universities[{name,count}], returning,
// past_seasons (ключа нет или 0 — строку не рисуем), city_label,
// cities[{city_label,total,pending,approved}].

import { flatRow, isCoreHandledError } from "../ui.js";

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
      h("h1", { text: "Цифры" }),
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
  root.replaceChildren(h("h1", { text: "Цифры" }), body);
  body.replaceChildren(
    figure("…", "Всего регистраций"),
    h("p", { class: "label-role", text: "Топ-3 вуза" }),
    h("div", { class: "home-figure", text: "…" }),
    figure("…", "Повторных"),
    h("p", { class: "label-role", text: "По городам" }),
    h("div", { class: "home-figure", text: "…" }),
  );

  let data;
  try {
    data = await api("/stats/registrations");
  } catch (err) {
    if (isCoreHandledError(err)) return;
    fail();
    return;
  }
  if (!data || typeof data !== "object") {
    fail();
    return;
  }

  const uniRows = [];
  const unis = Array.isArray(data.universities) ? data.universities.slice(0, 3) : [];
  for (const uni of unis) {
    const name = uni && uni.name;
    if (!name) continue;
    uniRows.push(flatRow(h, {
      title: String(name),
      trailing: h("div", { class: "home-figure", text: String(count(uni.count)) }),
    }));
  }

  const cityRows = [];
  const cities = Array.isArray(data.cities) ? data.cities : [];
  for (const city of cities) {
    const label = city && city.city_label;
    if (!label) continue;
    const total = count(city.total);
    const pending = count(city.pending);
    const approved = count(city.approved);
    cityRows.push(flatRow(h, {
      title: String(label),
      meta: "всего " + total + ", на модерации " + pending + ", одобрено " + approved,
    }));
  }

  const nodes = [];
  if (typeof data.city_label === "string" && data.city_label.trim()) {
    nodes.push(h("p", { class: "label-role", text: data.city_label.trim() }));
  }
  nodes.push(figure(count(data.total), "Всего регистраций"));
  nodes.push(h("p", { class: "label-role", text: "Топ-3 вуза" }));
  if (uniRows.length) nodes.push(h("div", { class: "flat-list" }, uniRows));
  nodes.push(figure(count(data.returning), "Повторных"));
  if (count(data.past_seasons) > 0) nodes.push(figure(count(data.past_seasons), "Прошлые сезоны"));
  nodes.push(h("p", { class: "label-role", text: "По городам" }));
  if (cityRows.length) nodes.push(h("div", { class: "flat-list" }, cityRows));
  body.replaceChildren(...nodes);
}
