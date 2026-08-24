// Настройки-лайт (закрытый белый список тумблеров, T-19-43, editorial-минимал 19.1-06): список
// тумблеров-строк (flatRow, D-11) с человеческой подписью из реестра — тап переключает и тут же
// перерисовывает список ответом сервера (фронт не гадает состояние); состояние — иконкой check
// (вкл/выкл прозрачностью) И словом «Включено/Выключено» справа, не только цветом. Тумблеры
// «Mini App включён» / «Только менеджерам» — отдельным блоком с рамкой --warn и надзаголовком,
// который объясняет риск: выключение спрячет приложение у всех, не только у того, кто нажал
// (тумблер обязан уметь выключать сам себя).

import { flatRow } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";

const DANGER_KEYS = new Set(["miniapp_enabled", "miniapp_staff_only"]);

function errorText(err, fallback) {
  if (err && err.payload && err.payload.text) return err.payload.text;
  return fallback;
}

function isAuthError(err) {
  return Boolean(err && (err.status === 401 || (err.status === 403 && err.reason !== "not_editable") || err.status === 503));
}

export async function render(root, params, ctx) {
  const { h, api } = ctx;

  const notice = h("p", { class: "chip accent hidden" });
  const list = h("div", { class: "flat-list" });
  const dangerList = h("div", { class: "flat-list danger-settings" });

  root.append(
    h("h1", { text: "Настройки" }),
    notice,
    list,
    h("h2", {}, icon("alert-triangle"), h("span", { text: " Mini App целиком" })),
    h("p", { class: "muted", text: "Ниже — тумблеры, которые касаются всех: выключение спрячет приложение у всех менеджеров и делегатов, не только у вас." }),
    dangerList,
  );

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "accent"}${text ? "" : " hidden"}`;
  }

  let busy = false;

  function draw(items) {
    list.replaceChildren();
    dangerList.replaceChildren();
    for (const item of items) {
      const target = DANGER_KEYS.has(item.key) ? dangerList : list;
      target.append(row(item));
    }
  }

  function row(item) {
    const on = item.value === "on";
    return flatRow(h, {
      icon: "check",
      title: item.label,
      trailing: on ? "Включено" : "Выключено",
      onClick: () => toggle(item),
      cls: `check-row${on ? " on" : ""}`,
    });
  }

  async function toggle(item) {
    if (busy) return;
    busy = true;
    const next = item.value === "on" ? "off" : "on";
    try {
      const items = await api("/admin/settings", { method: "POST", body: { key: item.key, value: next } });
      say(`${item.label}: ${next === "on" ? "включено" : "выключено"}.`, "success");
      haptic("success");
      draw(items);
    } catch (err) {
      if (!isAuthError(err)) say(errorText(err, "Не получилось сохранить — попробуйте ещё раз."), "warn");
    } finally {
      busy = false;
    }
  }

  let items;
  try {
    items = await api("/admin/settings");
  } catch (err) {
    if (!isAuthError(err)) root.append(h("p", { class: "error-inline", text: "Не удалось загрузить настройки — попробуйте ещё раз." }));
    return;
  }
  draw(items);
}
