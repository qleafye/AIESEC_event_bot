// Монеты вручную (экран 8 скетча Phase 16 в вебе): поиск получателя по @username/id/части
// имени, карточка получателя с балансом, quick-pick сумм ± из /admin/coins/presets, своя
// сумма, обязательная причина, шаг подтверждения «Кому · сколько · за что» и только потом
// отправка. Журнал ручных начислений — постранично ниже, «Показать ещё».
//
// Ошибки сервера — человеческим текстом из payload.text, не кодом ответа.

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
  const sign = item.delta >= 0 ? "+" : "";
  return h("div", { class: "row" },
    h("div", {},
      h("div", { text: `${item.recipient} · ${item.reason}` }),
      h("div", { class: "faint", text: `${item.when} · ${item.changed_by_name} · ${item.source_label}` }),
    ),
    h("span", { class: `delta ${item.delta >= 0 ? "plus" : "minus"}`, text: `${sign}${item.delta}` }),
  );
}

export async function render(root, params, ctx) {
  const { h, api, tg } = ctx;

  const notice = h("p", { class: "chip accent hidden" });
  const input = h("input", { class: "input", type: "text", placeholder: "@username, id или часть имени" });
  const results = h("div");
  const holder = h("div");
  const journalList = h("div", { class: "card list-card" });
  const journalFoot = h("div", { class: "list-foot" });

  root.append(
    h("h1", { text: "Монеты вручную" }),
    notice,
    h("div", { class: "field" }, h("label", { text: "Кому начислить" }), input),
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

  function haptic(kind) {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(kind || "success");
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
      journalList.append(h("div", { class: "empty", text: page.empty_text || "" }));
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
    return h("article", {
      class: "card clickable search-result", role: "button", tabindex: "0",
      onClick: () => selectRecipient(user),
      onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") selectRecipient(user); },
    },
      h("div", {},
        h("div", { class: "title", text: user.name }),
        h("div", { class: "faint", text: [user.username, user.city].filter(Boolean).join(" · ") || "—" }),
      ),
      h("span", { class: "chip accent", text: `${user.balance}🪙` }),
    );
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
    results.append(items.map(resultRow));
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

    holder.replaceChildren(
      h("article", { class: "card recipient-card" },
        h("div", { class: "title", text: user.name }),
        h("div", { class: "muted", text: [user.username, user.city].filter(Boolean).join(" · ") || "—" }),
        h("div", { class: "faint", text: `Баланс: ${user.balance}🪙` }),
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
    );
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
        say(`Готово: ${res.name} · ${sign}${delta}🪙 · новый баланс ${res.balance}🪙.`, "success");
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
        h("p", { text: `Сколько: ${sign}${delta}🪙` }),
        h("p", { text: `За что: ${reason}` }),
        h("button", { class: "btn", type: "button", text: "✅ Начислить", onClick: submit }),
        h("button", { class: "btn ghost", type: "button", text: "◀️ Назад", onClick: () => renderAmount(user) }),
      ),
    );
  }

  await loadJournal();
}
