// Настройки-лайт (закрытый белый список тумблеров, T-19-43, editorial-минимал 19.1-06): список
// тумблеров-строк (flatRow, D-11) с человеческой подписью из реестра — тап переключает и тут же
// перерисовывает список ответом сервера (фронт не гадает состояние); состояние — иконкой check
// (вкл/выкл прозрачностью) И словом «Включено/Выключено» справа, не только цветом. Тумблеры
// «Mini App включён» / «Только менеджерам» — отдельным блоком с рамкой --warn и надзаголовком,
// который объясняет риск: выключение спрячет приложение у всех, не только у того, кто нажал
// (тумблер обязан уметь выключать сам себя).
//
// 260824-8qw (MD-03): отнимающее направление этих двух тумблеров — второй тап через
// `.confirm-box` (тот же паттерн, что архив/удаление в task_edit.js). Сервер помечает
// отнимающее направление полем `item.confirm` (не null) — фронт не хранит НИ ЧИСЕЛ, НИ
// какого тумблера в какую сторону опасен: это решает `miniapp.routers.settings.DANGER_CONFIRM`.
// Текст последствий — тоже с сервера (реестр `miniapp_confirm_*_text`), в JS ни одного
// литерала предупреждения — иначе владелец не смог бы переформулировать его без деплоя.
// Возвращающее направление (включить обратно / вернуть делегатам доступ) остаётся в один тап.

import { flatRow } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";
import { errorText, isAuthError as isAuthErrorBase } from "../form.js";

const DANGER_KEYS = new Set(["miniapp_enabled", "miniapp_staff_only"]);

// errorText/isAuthError — перенесены в form.js (план 21-04, были дословным дублем task_edit.js).
// "not_editable" — единственная причина 403 у этого экрана, которая НЕ гейт авторизации;
// поведение не изменилось.
const AUTH_EXCEPT_REASONS = ["not_editable"];
function isAuthError(err) {
  return isAuthErrorBase(err, AUTH_EXCEPT_REASONS);
}

export async function render(root, params, ctx) {
  const { h, api } = ctx;

  const notice = h("p", { class: "chip accent hidden" });
  const list = h("div", { class: "flat-list" });
  const dangerList = h("div", { class: "flat-list danger-settings" });

  // Общая коробка подтверждения (D-11-паттерн task_edit.js) — одна на экран, содержимое
  // подставляется под конкретный тумблер перед показом. Живёт СРАЗУ ПОСЛЕ dangerList, не
  // внутри .flat-list — иначе разъедутся скругления/разделители списка.
  const confirmText = h("p", {});
  const confirmYes = h("button", { class: "btn danger", type: "button" });
  const confirmBox = h("div", { class: "confirm-box hidden" },
    confirmText,
    confirmYes,
    h("button", { class: "btn ghost", type: "button", text: "Отмена", onClick: () => confirmBox.classList.add("hidden") }),
  );

  root.append(
    h("h1", { text: "Настройки" }),
    notice,
    list,
    h("h2", {}, icon("alert-triangle"), h("span", { text: " Mini App целиком" })),
    h("p", { class: "muted", text: "Ниже — тумблеры, которые касаются всех: выключение спрячет приложение у всех менеджеров и делегатов, не только у вас." }),
    dangerList,
    confirmBox,
  );

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "accent"}${text ? "" : " hidden"}`;
  }

  let busy = false;

  function draw(items) {
    confirmBox.classList.add("hidden");  // перерисовка = сброс незавершённого подтверждения
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
      onClick: () => (item.confirm ? openConfirm(item) : toggle(item)),
      cls: `check-row${on ? " on" : ""}`,
    });
  }

  function openConfirm(item) {
    confirmText.textContent = item.confirm;
    confirmYes.textContent = item.value === "on" ? "Да, выключить" : "Да, включить";
    confirmYes.onclick = () => toggle(item);
    confirmBox.classList.remove("hidden");
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
