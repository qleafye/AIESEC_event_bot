// Экран списка #/admin-reject-rules.
// доменные тексты приходят с сервера; здесь только служебные строки экрана.

import { emptyState, errorState, errorText, guardedRender, noticeBox } from "../ui.js";
import { isAuthError } from "../form.js";
import { openSheet } from "../rr_sheet.js";
import { haptic } from "../motion.js";
import { icon } from "../icons.js";

const LOCKED_FALLBACK = "Для всех городов. Править может только руководитель без привязки к городу.";
const LOADING = "Загружаем правила…";
const LOAD_ERROR = "Не удалось загрузить правила. Проверьте интернет и повторите.";
const SAVE_ERROR = "Не получилось сохранить. Попробуйте ещё раз.";

let liveSheet = null;

export function unmount() {
  if (!liveSheet) return;
  const sheet = liveSheet;
  liveSheet = null;
  sheet.close();
}

function present(h, opts) {
  const userClose = opts.onClose;
  const sheet = openSheet(h, {
    ...opts,
    onClose() {
      if (liveSheet === sheet) liveSheet = null;
      if (userClose) userClose();
    },
  });
  liveSheet = sheet;
  return sheet;
}

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;
  const notice = noticeBox(h, { kind: "success", autoHideMs: 2400 });
  let city = null;
  let loadGen = 0;

  await reload();

  function chosenCity() {
    if (city) return city;
    if (me && me.city) return me.city;
    return null;
  }

  function listPath() {
    if (!city) return "/admin/reject-rules";
    return "/admin/reject-rules?city=" + encodeURIComponent(city);
  }

  async function reload() {
    const gen = ++loadGen;
    await guardedRender(root, ctx, () => draw(gen));
  }

  async function draw(gen) {
    root.append(h("p", { class: "rr-hint", text: LOADING }));
    let page;
    try {
      page = await api(listPath());
    } catch (err) {
      if (gen !== loadGen) return;
      if (isAuthError(err, ["out_of_scope", "not_found"])) throw err;
      root.replaceChildren(loadError());
      return;
    }
    if (gen !== loadGen) return;
    paint(page);
  }

  function loadError() {
    return h("div", {},
      errorState(h, { me, text: LOAD_ERROR }),
      h("button", {
        class: "btn secondary",
        type: "button",
        text: "Повторить загрузку",
        onClick: () => reload(),
      }),
    );
  }

  function paint(page) {
    const nodes = [
      h("h1", { class: "rr-title", text: "Правила автоотказа" }),
      h("p", { class: "rr-lead", text: "Бот сам отклоняет заявки, подходящие под правило." }),
      notice.el,
      masterBlock(page),
      cityChips(page),
      createRow(),
      page.items && page.items.length ? h("div", { class: "rr-list" }, ...page.items.map(ruleCard)) : emptyBlock(page),
      page.hint ? h("p", { class: "rr-hint", text: page.hint }) : null,
    ];
    root.replaceChildren(h("div", { class: "rr-screen" }, ...nodes.filter(Boolean)));
  }

  function masterBlock(page) {
    if (page.can_toggle_master) {
      const on = Boolean(page.master && page.master.enabled);
      return h("div", { class: "rr-master" },
        h("span", { text: "Автоотказ" }),
        h("button", {
          type: "button",
          class: "rr-switch",
          role: "switch",
          "aria-checked": on ? "true" : "false",
          "aria-label": "Автоотказ",
          onClick: () => (on ? askDisable() : enableMaster()),
        }),
      );
    }
    const status = page.master && page.master.status_text;
    return status ? h("p", { class: "rr-hint", text: status }) : null;
  }

  function cityChips(page) {
    if (!page.can_toggle_master || !Array.isArray(page.cities) || page.cities.length <= 1) return null;
    const chips = [
      chip("Все города", city == null, () => pickCity(null)),
      ...page.cities.map((item) => chip(item.label || "", city === item.code, () => pickCity(item.code))),
    ];
    return h("div", { class: "rr-chips" }, ...chips);
  }

  function chip(label, on, onClick) {
    return h("button", {
      type: "button",
      class: on ? "rr-chip is-on" : "rr-chip",
      "aria-pressed": on ? "true" : "false",
      text: label,
      onClick,
    });
  }

  function pickCity(code) {
    city = code;
    reload();
  }

  function createRow() {
    return h("div", {},
      h("button", {
        class: "btn rr-create",
        type: "button",
        onClick: openCreateMenu,
      }, icon("plus"), " Создать правило"),
      h("button", {
        class: "btn ghost",
        type: "button",
        text: "Взять заготовку",
        onClick: () => openPresets(),
      }),
    );
  }

  function openCreateMenu() {
    const sheet = present(h, {
      title: "Создать правило",
      actions: [
        { text: "Пустое правило", kind: "primary", onClick: () => { sheet.close(); goBlank(); } },
        { text: "Из заготовки", kind: "outlined", onClick: () => { sheet.close(); openPresets(); } },
      ],
    });
  }

  function goBlank() {
    const code = chosenCity();
    try {
      if (code) sessionStorage.setItem("rr_new_city", code);
      else sessionStorage.removeItem("rr_new_city");
    } catch (_) { /* хранилище закрыто — редактор спросит город сам */ }
    location.hash = "#/admin-reject-rule/new";
  }

  async function openPresets() {
    const host = h("div");
    host.append(h("p", { class: "rr-hint", text: "Загружаем заготовки…" }));
    const sheet = present(h, {
      title: "Заготовки",
      body: host,
      actions: [{ text: "Закрыть", kind: "ghost", onClick: () => sheet.close() }],
    });
    try {
      const catalog = await api(city
        ? `/admin/reject-rules/catalog?city=${encodeURIComponent(city)}`
        : "/admin/reject-rules/catalog");
      const rows = (catalog.presets || []).map((preset) => presetRow(sheet, preset));
      host.replaceChildren(...(rows.length ? rows : [h("p", { class: "rr-hint", text: "Заготовок нет." })]));
    } catch (err) {
      if (isAuthError(err, ["out_of_scope", "not_found"])) return;
      host.replaceChildren();
      sheet.setError(errorText(err, LOAD_ERROR));
    }
  }

  function presetRow(sheet, preset) {
    const name = h("div", { class: "rr-preset__name", text: preset.name || "" });
    const summary = h("div", { class: "rr-preset__summary", text: preset.summary || "" });
    if (!preset.available) {
      return h("div", { class: "rr-preset rr-preset--off" },
        name,
        summary,
        preset.unavailable_text ? h("p", { class: "rr-hint", text: preset.unavailable_text }) : null,
      );
    }
    return h("button", {
      type: "button",
      class: "rr-preset",
      onClick: () => createFromPreset(sheet, preset.index),
    }, name, summary);
  }

  async function createFromPreset(sheet, index) {
    sheet.setBusy(true);
    sheet.setError("");
    try {
      const res = await api("/admin/reject-rules", {
        method: "POST",
        body: { preset: index, city: chosenCity() },
      });
      const id = res && res.rule && res.rule.id;
      sheet.close();
      notice.say("Правило создано выключенным. Проверьте условия и включите");
      haptic("success");
      if (id != null) location.hash = "#/admin-reject-rule/" + id;
    } catch (err) {
      sheet.setBusy(false);
      sheet.setError(errorText(err, SAVE_ERROR));
    }
  }

  async function askDisable() {
    let res;
    try {
      res = await api("/admin/reject-rules/master", { method: "POST", body: { enabled: false } });
    } catch (err) {
      notice.say(errorText(err, SAVE_ERROR), "danger");
      return;
    }
    const confirm = res && res.needs_confirm;
    if (!confirm) {
      await reload();
      return;
    }
    const dangerText = confirm.enabled_count === 0 ? "Всё равно выключить" : "Да, выключить все правила";
    const sheet = present(h, {
      title: "Выключить автоотказ?",
      body: h("p", { text: confirm.text || "" }),
      warn: true,
      actions: [
        { text: "Не выключать", kind: "ghost", onClick: () => sheet.close() },
        { text: dangerText, kind: "danger", onClick: () => confirmDisable(sheet) },
      ],
    });
  }

  async function confirmDisable(sheet) {
    sheet.setBusy(true);
    sheet.setError("");
    try {
      await api("/admin/reject-rules/master", {
        method: "POST",
        body: { enabled: false, confirm: true },
      });
      sheet.close();
      notice.say("Автоотказ выключен");
      haptic("success");
      await reload();
    } catch (err) {
      sheet.setBusy(false);
      sheet.setError(errorText(err, SAVE_ERROR));
    }
  }

  async function enableMaster() {
    try {
      await api("/admin/reject-rules/master", { method: "POST", body: { enabled: true } });
      notice.say("Автоотказ включён");
      haptic("success");
      await reload();
    } catch (err) {
      notice.say(errorText(err, SAVE_ERROR), "danger");
    }
  }

  function selectedLabel(page) {
    const row = (page.cities || []).find((item) => item.code === city);
    return (row && row.label) || "этом городе";
  }

  function emptyBlock(page) {
    if (city != null) {
      const label = selectedLabel(page);
      return emptyState(h, {
        me,
        text: `В ${label} правил нет`,
        action: h("button", {
          class: "btn secondary",
          type: "button",
          text: `Создать правило для ${label}`,
          onClick: goBlank,
        }),
      });
    }
    return emptyState(h, {
      me,
      text: page.empty_text || "",
      action: h("div", { class: "rr-empty-actions" },
        h("button", { class: "btn", type: "button", text: "Взять заготовку", onClick: () => openPresets() }),
        h("button", { class: "btn secondary", type: "button", text: "Создать с нуля", onClick: goBlank }),
      ),
    });
  }

  function ruleCard(rule) {
    const title = rule.name || rule.summary || "Правило";
    const paused = rule.status === "paused";
    const locked = rule.can_edit === false;
    const cls = ["rr-card", locked ? "rr-card--muted" : "", paused ? "rr-card--paused" : ""].filter(Boolean).join(" ");
    const summary = rule.name && rule.summary ? h("div", { class: "rr-card__summary", text: rule.summary }) : null;
    const pause = paused && rule.paused_text ? h("p", { class: "rr-card__pause", text: rule.paused_text }) : null;
    const meta = h("div", { class: "rr-card__meta" },
      statusNode(rule),
      rule.city_label ? h("span", { text: rule.city_label }) : null,
      rule.action_label ? h("span", { text: rule.action_label }) : null,
      h("span", { text: "уже отклонило " + String(rule.rejected_count == null ? 0 : rule.rejected_count) }),
    );
    if (locked) {
      const phrase = rule.locked_text || LOCKED_FALLBACK;
      return h("div", { class: cls },
        h("button", {
          type: "button",
          class: "rr-card__hit",
          onClick: () => notice.say(phrase, "warn"),
        },
          h("div", { class: "rr-card__title", text: title }),
          meta,
          h("p", { class: "rr-card__lock", text: phrase }),
          pause,
        ),
        h("details", { class: "rr-card__more" },
          h("summary", { text: "Что делает правило" }),
          h("p", { text: rule.summary || "" }),
        ),
      );
    }
    return h("button", {
      type: "button",
      class: cls,
      "aria-label": `Открыть правило «${title}»`,
      onClick: () => { location.hash = "#/admin-reject-rule/" + rule.id; },
    },
      h("div", { class: "rr-card__title", text: title }),
      summary,
      meta,
      pause,
      h("span", { class: "rr-card__chev", "aria-hidden": "true" }, icon("chevron-right")),
    );
  }

  function statusNode(rule) {
    if (rule.status === "paused") {
      return h("span", { class: "rr-status rr-status--paused" }, icon("alert-triangle"), "На паузе");
    }
    if (rule.status === "on") {
      return h("span", { class: "rr-status rr-status--on" }, icon("check"), "Включено");
    }
    return h("span", { class: "rr-status" }, icon("circle"), "Выключено");
  }
}
