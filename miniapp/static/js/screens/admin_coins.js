// Монеты вручную (экран 8 скетча Phase 16 в вебе, editorial-минимал 19.1-06): поиск получателя
// по @username/id/части имени (поле с иконкой search и подсказкой формата), карточка получателя
// (единичный объект — остаётся карточкой, D-11) с балансом, quick-pick сумм ± из
// /admin/coins/presets, своя сумма, обязательная причина, шаг подтверждения «Кому · сколько ·
// за что» и только потом отправка. Журнал ручных начислений — плоские строки (D-11) постранично
// ниже, дельта + зелёным / − приглушённым (dataviz-контракт, план 19.1-05), пустой журнал —
// emptyState (D-18).
//
// Ошибки сервера — человеческим текстом из payload.text, не кодом ответа.

import { flatRow, emptyState } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";

const SEARCH_DEBOUNCE_MS = 300;
const JOURNAL_PAGE = 20;

function errorText(err, fallback) {
  if (err && err.payload && err.payload.text) return err.payload.text;
  return fallback;
}

function isAuthError(err) {
  return Boolean(err && (err.status === 401 || (err.status === 403 && err.reason !== "out_of_scope" && err.reason !== "not_found") || err.status === 503));
}

function journalRow(h, item) {
  const delta = item.delta >= 0 ? `+${item.delta}` : String(item.delta);
  return flatRow(h, {
    title: `${item.recipient} · ${item.reason}`,
    meta: `${item.when} · ${item.changed_by_name} · ${item.source_label}`,
    trailing: h("span", { class: `delta ${item.delta >= 0 ? "plus" : "minus"}`, text: delta }),
  });
}

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;

  const notice = h("p", { class: "chip accent hidden" });
  const input = h("input", { class: "input", type: "text", placeholder: "@username, id или часть имени" });
  const results = h("div");
  const holder = h("div");
  const journalList = h("div", { class: "flat-list" });
  const journalFoot = h("div", { class: "list-foot" });

  root.append(
    h("h1", { text: "Монеты вручную" }),
    notice,
    h("div", { class: "field" },
      h("label", { text: "Кому начислить" }),
      h("div", { class: "search-field" }, icon("search"), input),
    ),
    results,
    holder,
    h("h2", { text: "Журнал" }),
    journalList,
    journalFoot,
  );

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "accent"}${text ? "" : " hidden"}`;
  }

  let presets = null;
  async function getPresets() {
    if (!presets) presets = await api("/admin/coins/presets");
    return presets;
  }

  // ── журнал ──
  let offset = 0;
  let total = 0;
  async function loadJournal() {
    journalFoot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    let page;
    try {
      page = await api(`/admin/coins?offset=${offset}&limit=${JOURNAL_PAGE}`);
    } catch (err) {
      journalFoot.replaceChildren();
      if (!isAuthError(err)) journalFoot.append(h("p", { class: "error-inline", text: "Не удалось загрузить журнал — попробуйте ещё раз." }));
      return;
    }
    total = page.total;
    for (const item of page.items) journalList.append(journalRow(h, item));
    offset += page.items.length;
    journalFoot.replaceChildren();
    if (total === 0) {
      journalList.replaceChildren(emptyState(h, { me, text: page.empty_text || "" }));
      return;
    }
    if (offset < total) {
      journalFoot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: loadJournal,
      }));
    }
  }

  function resetJournal() {
    offset = 0;
    total = 0;
    journalList.replaceChildren();
    return loadJournal();
  }

  // ── поиск получателя ──
  let searchTimer = null;

  function resultRow(user) {
    return flatRow(h, {
      title: user.name,
      meta: [user.username, user.city].filter(Boolean).join(" · ") || "—",
      trailing: h("span", { class: "chip accent" }, icon("coin"), h("span", { text: String(user.balance) })),
      onClick: () => selectRecipient(user),
      cls: "search-result",
    });
  }

  async function search(q) {
    const needle = q.trim();
    results.replaceChildren();
    if (needle.length < 2) return;
    let items;
    try {
      items = await api(`/admin/users/search?q=${encodeURIComponent(needle)}`);
    } catch (err) {
      if (!isAuthError(err)) results.append(h("p", { class: "error-inline", text: "Не удалось найти — попробуйте ещё раз." }));
      return;
    }
    if (input.value.trim() !== needle) return; // ушли дальше, пока грузили
    if (items.length === 0) {
      results.append(h("p", { class: "muted", text: "Никого не нашли — проверьте написание." }));
      return;
    }
    results.append(h("div", { class: "flat-list" }, items.map(resultRow)));
  }

  input.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    const value = input.value;
    searchTimer = setTimeout(() => search(value), SEARCH_DEBOUNCE_MS);
  });

  function selectRecipient(user) {
    results.replaceChildren();
    say("");
    renderAmount(user);
  }

  // ── карточка получателя: quick-pick сумм, своя сумма, причина ──
  async function renderAmount(user) {
    let busy = false;
    const info = await getPresets().catch(() => ({ presets: [], delta_max: null, reason_max: null }));

    const customInput = h("input", { class: "input", type: "number", step: "1", inputmode: "numeric", placeholder: "например -5" });
    const reasonInput = h("textarea", {
      class: "input", rows: "2", placeholder: "Причина — коротко, за что",
      maxlength: info.reason_max ? String(info.reason_max) : undefined,
    });

    function pick(delta) {
      customInput.value = String(delta);
    }

    function toConfirm() {
      if (busy) return;
      const delta = Number(customInput.value);
      if (!Number.isInteger(delta) || delta === 0) {
        say("Сумма — целое число не ноль, например 5 или -5.", "warn");
        return;
      }
      if (info.delta_max && Math.abs(delta) > info.delta_max) {
        say(`Сумма по модулю не больше ${info.delta_max}.`, "warn");
        return;
      }
      const reason = reasonInput.value.trim();
      if (!reason) {
        say("Причина обязательна — коротко, за что начисляем.", "warn");
        return;
      }
      say("");
      renderConfirm(user, delta, reason);
    }

    const buttons = [];
    for (const n of info.presets || []) {
      buttons.push(h("button", { class: "btn secondary", type: "button", text: `+${n}`, onClick: () => pick(n) }));
      buttons.push(h("button", { class: "btn secondary", type: "button", text: `−${n}`, onClick: () => pick(-n) }));
    }

    holder.replaceChildren(...[
      h("article", { class: "card recipient-card" },
        h("div", { class: "title", text: user.name }),
        h("div", { class: "muted", text: [user.username, user.city].filter(Boolean).join(" · ") || "—" }),
        h("div", { class: "flat-row-meta" }, icon("coin"), h("span", { text: ` Баланс: ${user.balance}` })),
      ),
      buttons.length ? h("div", { class: "choice-grid" }, buttons) : null,
      h("div", { class: "field" },
        h("label", { text: "Своя сумма (можно со знаком минус — тогда спишет)" }),
        customInput,
      ),
      h("div", { class: "field" },
        h("label", { text: "Причина — обязательна, увидит только менеджер в журнале" }),
        reasonInput,
      ),
      h("div", { class: "task-actions" },
        h("button", { class: "btn", type: "button", text: "Далее →", onClick: toConfirm }),
        h("button", { class: "btn ghost", type: "button", text: "Отмена", onClick: () => holder.replaceChildren() }),
      ),
    ].filter(Boolean));
  }

  // ── подтверждение: «Кому · сколько · за что» — только потом отправка ──
  function renderConfirm(user, delta, reason) {
    let busy = false;
    const sign = delta >= 0 ? "+" : "";

    async function submit() {
      if (busy) return;
      busy = true;
      try {
        const res = await api("/admin/coins", { method: "POST", body: { user_id: user.telegram_id, delta, reason } });
        haptic("success");
        say(`Готово: ${res.name} · ${sign}${delta} · новый баланс ${res.balance}.`, "success");
        holder.replaceChildren();
        await resetJournal();
      } catch (err) {
        busy = false;
        if (!isAuthError(err)) say(errorText(err, "Не получилось начислить — попробуйте ещё раз."), "warn");
      }
    }

    holder.replaceChildren(
      h("div", { class: "confirm-box" },
        h("p", { text: `Кому: ${user.name}` }),
        h("p", { text: `Сколько: ${sign}${delta}` }),
        h("p", { text: `За что: ${reason}` }),
        h("button", { class: "btn", type: "button", text: "Начислить", onClick: submit }),
        h("button", { class: "btn ghost", type: "button", text: "Назад", onClick: () => renderAmount(user) }),
      ),
    );
  }

  await loadJournal();
}
