// Экран редактора #/admin-reject-rule/{id}. id "new" — ещё не записанное правило.
// Черновик копится здесь и уходит на сервер только по «Сохранить правило».

import { confirmBox, diffView, field, isAuthError, setFieldState } from "../form.js";
import { errorState, errorText, guardedRender, noticeBox } from "../ui.js";
import { renderConditions, validateGroups } from "../rr_conditions.js";
import { openSheet } from "../rr_sheet.js";
import { countUp, haptic } from "../motion.js";
import { icon } from "../icons.js";
import { suspendBackButton } from "../app.js";

const LOCKED = "Это правило относится к другому городу. Править его может руководитель без привязки к городу.";
const SAVE_NET = "Не получилось сохранить. Черновик остался на этом устройстве — попробуйте ещё раз.";
const REJECT_EMPTY = "Напишите, что получит делегат, — без текста отказ не отправить.";
const NAME_HELP = "Для себя, чтобы узнавать правило в списке. Например: «Не берём школьников»";
const REJECT_HELP = "Это увидит делегат. Например: «Спасибо за интерес к форуму. К сожалению, мы не можем принять вашу заявку: форум только для студентов вузов.» Текст автоматически переведётся на английский.";
const COUNT_HINT = "Считаются все текущие заявки; правило применится только к новым и пересланным";
const WIDE = "Правило очень широкое — под него попадает больше половины заявок.";
const WIDE_ENABLE = "Это больше половины всех заявок. Убедитесь, что условия собраны верно.";
const COPY_NOTE = "Копия будет выключена — проверьте её и включите сами.";
const CREATED = "Правило создано выключенным. Проверьте условия и включите";

let cleanup = null;

export function unmount() {
  if (!cleanup) return;
  const run = cleanup;
  cleanup = null;
  run();
}

function blankCondition() {
  return { step: "", op: "", values: [] };
}

function cloneGroups(groups) {
  return (groups || []).map((group) => (group || []).map((cond) => ({
    step: cond && cond.step ? String(cond.step) : "",
    op: cond && cond.op ? String(cond.op) : "",
    values: [...((cond && cond.values) || [])].map((value) => String(value)),
  })));
}

function snapshot(source) {
  return JSON.stringify({
    name: (source.name || "").trim(),
    city: source.city || null,
    tracks: [...(source.tracks || [])],
    conditions: cloneGroups(source.conditions),
    action: source.action || "reject",
    reject_text: (source.reject_text || "").trim(),
  });
}

function fromRule(rule) {
  const groups = cloneGroups(rule.conditions);
  return {
    name: rule.name || "",
    city: rule.city || null,
    tracks: rule.tracks && rule.tracks.length ? [...rule.tracks] : ["full"],
    conditions: groups.length ? groups : [],
    action: rule.action || "reject",
    reject_text: rule.reject_text || "",
    enabled: rule.enabled ? 1 : 0,
  };
}

function storageKey(id) {
  return `rr_draft:${id || "new"}`;
}

function catalogPath(city, tracks) {
  const query = new URLSearchParams();
  if (city) query.set("city", city);
  const list = tracks && tracks.length ? tracks : ["full"];
  query.set("tracks", list.join(","));
  return `/admin/reject-rules/catalog?${query.toString()}`;
}

function readJson(key) {
  try {
    const raw = sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

function writeJson(key, value) {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch (_) { /* хранилище закрыто — черновик живёт только на экране */ }
}

function removeKey(key) {
  try { sessionStorage.removeItem(key); } catch (_) { /* нет доступа */ }
}

function takeFlash() {
  const flash = readJson("rr_flash");
  removeKey("rr_flash");
  return flash && flash.text ? String(flash.text) : "";
}

function putFlash(text) {
  writeJson("rr_flash", { text });
}

function whenText(stamp) {
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export async function render(root, params, ctx) {
  const { h, api, me, setMainButton, showState } = ctx;
  const isNew = params.id === "new";
  const notice = noticeBox(h, { kind: "success", autoHideMs: 2400 });
  let server = { rev: null, enabled: 0, status: "off", summary: "", can_edit: true, conditions: [] };
  let draft = fromRule({ tracks: ["full"], conditions: [[blankCondition()]], action: "reject" });
  let catalog = { steps: [], cities: [], tracks: [], forum_date: {} };
  let readOnly = false;
  let busy = false;
  let conditionsUi = null;
  let rejectField = null;
  let saveError = null;
  let counter = null;
  let wideNote = null;
  let summaryText = null;
  let saveButton = null;
  let resetButton = null;
  let trackBox = null;
  let draftTimer = null;
  let previewTimer = null;
  let previewGen = 0;
  let catalogGen = 0;
  let previewAbort = null;
  let lastMatched = 0;
  let restoreBack = null;
  let sheet = null;
  let alive = true;

  cleanup = () => {
    alive = false;
    if (draftTimer) clearTimeout(draftTimer);
    if (previewTimer) clearTimeout(previewTimer);
    if (previewAbort) previewAbort.abort();
    if (sheet) sheet.close();
    if (restoreBack) restoreBack();
    setMainButton(null);
  };

  await guardedRender(root, ctx, load);

  function authStop(err) {
    if (err && (err.status === 401 || (err.status === 503 && err.reason === "miniapp_off"))) return true;
    return false;
  }

  async function load() {
    const flash = takeFlash();
    try {
      if (isNew) {
        const city = readNewCity();
        draft = fromRule({
          city, tracks: ["full"], conditions: [[blankCondition()]], action: "reject", enabled: 0,
        });
        server = {
          ...draft,
          rev: null,
          status: "off",
          summary: "",
          can_edit: true,
          tracks: [...draft.tracks],
          conditions: cloneGroups(draft.conditions),
        };
        catalog = await api(catalogPath(draft.city, draft.tracks), { quiet: true });
      } else {
        const got = await api(`/admin/reject-rules/${params.id}`, { quiet: true });
        server = got.rule || server;
        draft = fromRule(server);
        readOnly = server.can_edit === false;
        catalog = await api(catalogPath(draft.city, draft.tracks), { quiet: true });
      }
    } catch (err) {
      if (!alive) return;
      if (authStop(err)) throw err;
      if (err && err.status === 403 && err.reason !== "out_of_scope") {
        showState("no-access", err.payload && err.payload.text);
        return;
      }
      if (err && err.status === 403) return paintLocked(errorText(err, LOCKED));
      if (err && err.status === 404) return paintMissing();
      if (isAuthError(err, ["out_of_scope", "not_found"])) throw err;
      return paintLoadError();
    }
    if (!alive) return;
    paint(flash);
  }

  function readNewCity() {
    try {
      return sessionStorage.getItem("rr_new_city") || null;
    } catch (_) {
      return (me && me.city) || null;
    }
  }

  function dirty() {
    return snapshot(draft) !== snapshot(fromRule(server));
  }

  function forumDate() {
    const box = catalog.forum_date || {};
    return box.date ? String(box.date) : null;
  }

  function cityName(code) {
    const row = (catalog.cities || []).find((item) => (item.code || null) === (code || null));
    if (row && row.label) return row.label;
    if (!code) return "Все города";
    return server.city_label || "";
  }

  function trackText(codes) {
    const labels = [];
    for (const code of codes || []) {
      const row = (catalog.tracks || []).find((item) => item.code === code);
      if (row && row.label) labels.push(row.label);
    }
    return labels.join(", ");
  }

  function payload() {
    return {
      name: (draft.name || "").trim() || null,
      city: draft.city || null,
      tracks: [...draft.tracks],
      conditions: cloneGroups(draft.conditions),
      action: draft.action || "reject",
      reject_text: (draft.reject_text || "").trim() || null,
    };
  }

  function remember() {
    if (readOnly) return;
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      writeJson(storageKey(params.id), {
        draft: payload(),
        base_rev: server.rev || null,
        saved_at: Date.now(),
      });
    }, 400);
  }

  function forget() {
    if (draftTimer) clearTimeout(draftTimer);
    removeKey(storageKey(params.id));
  }

  function touch() {
    remember();
    schedulePreview();
    syncBar();
  }

  function paint(flash) {
    const stored = readJson(storageKey(params.id));
    const offer = stored && stored.draft && snapshot(stored.draft) !== snapshot(draft);
    const status = statusBadge();
    const head = h("div", { class: "rr-block-head" },
      h("h1", { class: "rr-title", text: "Правило" }),
      status,
    );
    const blocks = [notice.el, head];
    if (server.status === "paused" && server.paused_text) {
      blocks.push(h("div", { class: "rr-warn" }, h("p", { text: server.paused_text })));
    }
    if (readOnly) blocks.push(h("p", { class: "rr-hint", text: LOCKED }));
    if (offer) blocks.push(restoreOffer(stored));
    if (!isNew && !readOnly) blocks.push(statusRow());
    blocks.push(h("p", { class: "rr-eyebrow", text: "Основное" }));
    blocks.push(nameField());
    blocks.push(cityField());
    blocks.push(h("p", { class: "rr-hint", text: "Для каких заявок" }));
    trackBox = h("div", { class: "rr-chips" });
    refreshTracks();
    blocks.push(trackBox);
    blocks.push(h("p", { class: "rr-eyebrow", text: "Когда срабатывает" }));
    conditionsUi = renderConditions(h, {
      groups: draft.conditions,
      catalog,
      cityLabel: cityName(draft.city),
      forumDate: forumDate(),
      readOnly,
      onChange(groups) {
        draft.conditions = cloneGroups(groups);
        touch();
      },
    });
    blocks.push(conditionsUi.el);
    summaryText = h("p", { text: server.summary || "" });
    blocks.push(h("div", { class: "rr-card" },
      h("p", { class: "rr-hint", text: "Правило читается так:" }),
      summaryText,
    ));
    blocks.push(h("p", { class: "rr-eyebrow", text: "Что делать" }));
    blocks.push(actionControl());
    rejectField = field(h, {
      key: "reject_text",
      type: "textarea",
      label: "Текст отказа",
      help: REJECT_HELP,
      max_len: 1000,
    }, draft.reject_text, (value) => {
      draft.reject_text = value;
      setFieldState(rejectField, "default");
      touch();
    });
    if (draft.action !== "reject") rejectField.hidden = true;
    if (readOnly) setFieldState(rejectField, "disabled");
    blocks.push(rejectField);
    blocks.push(copyRow());
    if (!isNew && !readOnly) blocks.push(deleteRow());
    const editor = h("div", { class: "rr-editor" }, ...blocks);
    const pieces = [editor];
    if (!readOnly) pieces.push(bar());
    root.replaceChildren(...pieces);
    if (flash) notice.say(flash, "success");
    syncBar();
    schedulePreview();
    bindBack();
  }

  function statusBadge() {
    if (isNew || !server.enabled) return h("span", { class: "rr-status", text: "Выключено" });
    if (server.status === "paused") return h("span", { class: "rr-status rr-status--paused", text: "На паузе" });
    return h("span", { class: "rr-status rr-status--on", text: "Включено" });
  }

  function statusRow() {
    const on = Boolean(server.enabled);
    return h("div", { class: "rr-block-head" },
      h("span", { class: "rr-hint", text: on ? "Правило работает" : "Правило выключено" }),
      h("button", {
        type: "button",
        class: "btn secondary",
        text: on ? "Выключить" : "Включить",
        onClick: () => { if (on) disableRule(); else enableRule(); },
      }),
    );
  }

  function restoreOffer(stored) {
    const when = whenText(stored.saved_at);
    return h("div", { class: "rr-warn" },
      h("p", { text: `Нашли ваши несохранённые правки от ${when}.` }),
      h("div", { class: "rr-empty-actions" },
        h("button", {
          type: "button", class: "btn", text: "Продолжить",
          onClick: () => {
            draft = fromRule({ ...stored.draft, enabled: server.enabled, summary: server.summary });
            paint("");
          },
        }),
        h("button", {
          type: "button", class: "btn ghost", text: "Начать заново",
          onClick: () => { forget(); paint(""); },
        }),
      ),
    );
  }

  function nameField() {
    const node = field(h, {
      key: "name", type: "text", label: "Название", help: NAME_HELP, max_len: 60,
    }, draft.name, (value) => {
      draft.name = value;
      touch();
    });
    if (readOnly) setFieldState(node, "disabled");
    return node;
  }

  function cityField() {
    const bound = Boolean(me && me.city);
    const select = h("select", { class: "input rr-input" },
      ...(catalog.cities || []).map((item) => h("option", {
        value: item.code || "",
        text: item.label || "",
      })),
    );
    select.value = draft.city || "";
    if (bound || readOnly) select.disabled = true;
    select.addEventListener("change", () => {
      draft.city = select.value || null;
      touch();
      reloadCatalog();
    });
    return h("label", { class: "rr-field" },
      h("span", { class: "rr-hint", text: "Город" }),
      select,
      bound ? h("p", { class: "rr-hint", text: "Правила для всех городов может менять только руководитель без привязки к городу" }) : null,
    );
  }

  function refreshTracks() {
    if (!trackBox) return;
    trackBox.replaceChildren(...(catalog.tracks || []).map((item) => {
      const on = draft.tracks.includes(item.code);
      return h("button", {
        type: "button",
        class: on ? "rr-chip is-on" : "rr-chip",
        role: "checkbox",
        "aria-checked": on ? "true" : "false",
        disabled: readOnly ? true : null,
        text: item.label || "",
        onClick: () => toggleTrack(item.code),
      });
    }));
  }

  function toggleTrack(code) {
    if (readOnly) return;
    const has = draft.tracks.includes(code);
    if (has && draft.tracks.length === 1) {
      notice.say("Выберите хотя бы один тип заявки", "warn");
      return;
    }
    draft.tracks = has ? draft.tracks.filter((item) => item !== code) : [...draft.tracks, code];
    refreshTracks();
    touch();
    reloadCatalog();
  }

  async function reloadCatalog() {
    const gen = ++catalogGen;
    try {
      const next = await api(catalogPath(draft.city, draft.tracks), { quiet: true });
      if (!alive || gen !== catalogGen) return;
      catalog = next;
      refreshTracks();
      if (conditionsUi) {
        conditionsUi.update(draft.conditions, catalog, {
          cityLabel: cityName(draft.city),
          forumDate: forumDate(),
          readOnly,
        });
      }
    } catch (err) {
      if (!alive || authStop(err)) return;
      notice.say(errorText(err, "Не удалось обновить список вопросов"), "danger");
    }
  }

  function actionControl() {
    const help = h("p", {
      class: "rr-hint",
      text: draft.action === "flag"
        ? "Заявка останется на модерации с пометкой для менеджера. Делегату ничего не отправляется."
        : "Делегат получит сообщение об отказе, заявка закроется.",
    });
    function choice(action, label, glyph) {
      const on = draft.action === action;
      return h("button", {
        type: "button",
        role: "radio",
        class: "btn ghost",
        "aria-checked": on ? "true" : "false",
        "data-action": action,
        disabled: readOnly ? true : null,
        onClick: () => {
          if (readOnly || draft.action === action) return;
          draft.action = action;
          help.textContent = action === "flag"
            ? "Заявка останется на модерации с пометкой для менеджера. Делегату ничего не отправляется."
            : "Делегат получит сообщение об отказе, заявка закроется.";
          if (rejectField) rejectField.hidden = action !== "reject";
          for (const button of seg.querySelectorAll("[role=radio]")) {
            button.setAttribute("aria-checked", button.dataset.action === action ? "true" : "false");
          }
          touch();
        },
      }, icon(glyph), h("span", { text: label }));
    }
    const seg = h("div", { class: "rr-seg", role: "radiogroup" },
      choice("reject", "Отклонять", "ban"),
      choice("flag", "Только пометить", "flag"),
    );
    return h("div", { class: "rr-field" }, seg, help);
  }

  function copyRow() {
    if (isNew) {
      return h("div", { class: "rr-card rr-card--muted" },
        h("p", { class: "rr-card__title", text: "Скопировать в другой город" }),
        h("p", { class: "rr-hint", text: "Сначала сохраните правило" }),
      );
    }
    return h("button", {
      type: "button",
      class: "rr-create",
      onClick: openCopy,
    }, icon("copy"), h("span", { text: "Скопировать в другой город" }), icon("chevron-right"));
  }

  function deleteRow() {
    return h("button", {
      type: "button",
      class: "btn ghost",
      onClick: openDelete,
    }, icon("trash-2"), h("span", { class: "rr-error", text: "Удалить правило" }));
  }

  function bar() {
    counter = h("div", { "aria-live": "polite", text: "Добавьте условие — тогда посчитаем" });
    wideNote = h("div", { class: "rr-warn", hidden: true });
    saveError = h("p", { class: "rr-error", hidden: true });
    saveButton = h("button", {
      type: "button", class: "btn", text: "Сохранить правило", onClick: () => save(),
    });
    resetButton = h("button", {
      type: "button", class: "btn ghost", text: "Сбросить правки", hidden: true, onClick: askReset,
    });
    return h("div", { class: "rr-bar" },
      counter,
      wideNote,
      h("p", { class: "rr-hint", text: COUNT_HINT }),
      saveError,
      h("div", { class: "rr-empty-actions" }, saveButton, resetButton),
    );
  }

  function syncBar() {
    if (saveButton) saveButton.disabled = busy || !dirty();
    if (resetButton) resetButton.hidden = !dirty();
    if (!readOnly && dirty()) setMainButton("Сохранить правило", () => save(), { disabled: busy });
    else setMainButton(null);
  }

  function setSaveError(text) {
    if (!saveError) return;
    saveError.textContent = text || "";
    if (text) saveError.removeAttribute("hidden");
    else saveError.setAttribute("hidden", "");
  }

  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 600);
  }

  async function runPreview() {
    if (!alive || readOnly || !counter) return;
    const check = validateGroups(draft.conditions, catalog);
    const empty = !(draft.conditions || []).some((group) => group && group.length);
    if (!check.ok || empty) {
      counter.textContent = "Добавьте условие — тогда посчитаем";
      showWide(false);
      return;
    }
    if (previewAbort) previewAbort.abort();
    previewAbort = new AbortController();
    const gen = ++previewGen;
    counter.textContent = "Считаем…";
    try {
      const result = await api("/admin/reject-rules/preview", {
        method: "POST",
        body: payload(),
        signal: previewAbort.signal,
        quiet: true,
      });
      if (!alive || gen !== previewGen) return;
      paintCount(result);
    } catch (err) {
      if (!alive || (err && err.name === "AbortError") || gen !== previewGen) return;
      counter.textContent = lastMatched
        ? `Попали бы ${lastMatched} — устарело`
        : "Не удалось посчитать";
      const again = h("button", {
        type: "button", class: "btn ghost", text: "Повторить расчёт", onClick: () => schedulePreview(),
      });
      counter.append(document.createTextNode(" "), again);
    }
  }

  function paintCount(result) {
    showWide(Boolean(result && result.too_wide));
    if (result && result.summary && summaryText) summaryText.textContent = result.summary;
    if (result && result.forum_date_missing) {
      counter.textContent = result.text || "Не посчитать: у города не указана дата форума, а в правиле есть условие по возрасту.";
      return;
    }
    if (result && result.matched != null && result.total != null) {
      counter.textContent = "";
      const num = h("span", { class: "rr-count", text: "0" });
      counter.append(
        document.createTextNode("Попали бы "),
        num,
        document.createTextNode(` из ${result.total} заявок`),
      );
      countUp(num, lastMatched, result.matched);
      lastMatched = result.matched;
      return;
    }
    counter.textContent = (result && result.text) || "Добавьте условие — тогда посчитаем";
  }

  function showWide(on) {
    if (!wideNote) return;
    wideNote.textContent = on ? WIDE : "";
    if (on) wideNote.removeAttribute("hidden");
    else wideNote.setAttribute("hidden", "");
  }

  function readyToSend() {
    const check = validateGroups(draft.conditions, catalog);
    if (!check.ok) {
      if (conditionsUi) conditionsUi.focusFirstError();
      return false;
    }
    if ((draft.action || "reject") === "reject" && !(draft.reject_text || "").trim()) {
      setFieldState(rejectField, "error", { text: REJECT_EMPTY });
      const area = rejectField && rejectField.querySelector("textarea");
      if (area) area.focus();
      return false;
    }
    return true;
  }

  async function save(forceRev) {
    if (busy || readOnly || !dirty()) return;
    if (!readyToSend()) return;
    busy = true;
    syncBar();
    setSaveError("");
    if (sheet) sheet.setBusy(true);
    try {
      if (isNew) {
        const created = await api("/admin/reject-rules", {
          method: "POST", body: { draft: payload() }, quiet: true,
        });
        forget();
        putFlash(CREATED);
        haptic("success");
        location.hash = `#/admin-reject-rule/${created.rule.id}`;
        return;
      }
      const rev = forceRev || server.rev;
      const saved = await api(`/admin/reject-rules/${params.id}`, {
        method: "PUT",
        body: { ...payload(), base_rev: rev },
        quiet: true,
      });
      adopt(saved.rule);
      notice.say("Сохранено", "success");
      haptic("success");
      if (sheet) { sheet.close(); sheet = null; }
    } catch (err) {
      if (sheet) sheet.setBusy(false);
      if (err && err.status === 409 && err.reason === "stale") {
        openStale(err.payload || {});
        return;
      }
      if (err && err.status === 404) {
        openDeleted();
        return;
      }
      if (err && err.status) setSaveError(errorText(err, SAVE_NET));
      else notice.say(SAVE_NET, "danger");
    } finally {
      busy = false;
      if (alive) syncBar();
    }
  }

  function adopt(rule) {
    if (!rule) return;
    server = rule;
    draft = fromRule(rule);
    forget();
    paint("");
  }

  function humanSide(source, kind) {
    const same = JSON.stringify(cloneGroups(source.conditions)) === JSON.stringify(cloneGroups(server.conditions));
    let about = source.summary || server.summary || "без описания";
    if (kind === "draft" && !same) about = "Условия в черновике другие";
    return {
      "Название": source.name || "без названия",
      "Город": source.city_label || cityName(source.city),
      "Типы заявок": trackText(source.tracks) || "не выбраны",
      "Условия": about,
      "Действие": source.action_label || (source.action === "flag" ? "Только пометить" : "Отклонять"),
      "Текст отказа": source.reject_text || "нет текста",
      "Включено": source.enabled ? "включено" : "выключено",
    };
  }

  function openStale(body) {
    const fresh = body.rule || {};
    const mine = humanSide(draft, "draft");
    const theirs = humanSide(fresh, "fresh");
    const savedLines = [];
    for (const key of Object.keys(mine)) {
      if (mine[key] === theirs[key]) continue;
      savedLines.push(h("p", { text: `Сейчас сохранено: ${theirs[key]}` }));
    }
    present({
      title: "Правило уже изменили",
      warn: true,
      body: [
        h("p", { text: body.text || "" }),
        h("p", { class: "rr-eyebrow", text: "Что изменилось" }),
        diffView(h, mine, theirs, { wasPrefix: "У вас в черновике: " }),
        ...savedLines,
      ],
      actions: [
        {
          text: "Загрузить новую версию",
          kind: "outlined",
          note: "Ваши несохранённые правки пропадут",
          onClick: () => {
            adopt(fresh);
            notice.say("Загружена новая версия", "success");
          },
        },
        {
          text: "Сохранить мою поверх",
          kind: "outlined",
          note: "Чужие изменения пропадут",
          onClick: () => save(fresh.rev),
        },
      ],
    });
  }

  function openDeleted() {
    present({
      title: "Правило удалили",
      body: h("p", { text: "Это правило уже удалили. Ваши правки можно сохранить как новое правило (оно будет выключено)." }),
      actions: [
        { text: "Сохранить как новое", kind: "primary", onClick: () => saveAsNew() },
        { text: "Закрыть", kind: "ghost", onClick: () => { if (sheet) sheet.close(); } },
      ],
    });
  }

  async function saveAsNew() {
    if (busy) return;
    busy = true;
    if (sheet) sheet.setBusy(true);
    try {
      const created = await api("/admin/reject-rules", {
        method: "POST", body: { draft: payload() }, quiet: true,
      });
      forget();
      putFlash(CREATED);
      location.hash = `#/admin-reject-rule/${created.rule.id}`;
    } catch (err) {
      if (sheet) sheet.setError(errorText(err, SAVE_NET));
    } finally {
      busy = false;
    }
  }

  async function enableRule() {
    if (isNew || readOnly || busy) return;
    if (dirty()) {
      notice.say("Сначала сохраните правки — потом включим", "warn");
      return;
    }
    busy = true;
    try {
      const result = await api(`/admin/reject-rules/${params.id}/enable`, {
        method: "POST",
        body: { confirm: false, base_rev: server.rev },
        quiet: true,
      });
      if (result && result.needs_confirm) openEnable(result.needs_confirm);
    } catch (err) {
      if (err && err.status === 409) openStale(err.payload || {});
      else if (err && err.status === 400) openEnableError(errorText(err, ""));
      else notice.say(errorText(err, SAVE_NET), "danger");
    } finally {
      busy = false;
    }
  }

  function openEnable(confirm) {
    const body = [
      h("p", { class: "rr-card__title", text: draft.name || server.summary || "Правило" }),
      h("p", { text: server.summary || "" }),
    ];
    if (confirm.forum_date_missing) {
      body.push(h("div", { class: "rr-warn" }, h("p", { text: confirm.text || "" })));
    } else if (confirm.matched != null) {
      const num = h("span", { class: "rr-count", text: String(confirm.matched) });
      body.push(h("p", {}, num, ` из ${confirm.total} под правило попали бы заявки`));
      body.push(h("p", { text: confirm.text || "" }));
    } else {
      body.push(h("p", { text: confirm.text || "" }));
    }
    if (confirm.too_wide) body.push(h("div", { class: "rr-warn" }, h("p", { text: WIDE_ENABLE })));
    const actions = confirm.forum_date_missing
      ? [
        { text: "Не включать", kind: "ghost", onClick: () => sheet && sheet.close() },
        { text: "Включить всё равно", kind: "outlined", onClick: () => confirmEnable() },
      ]
      : [
        { text: "Включить правило", kind: "primary", onClick: () => confirmEnable() },
        { text: "Не включать", kind: "ghost", onClick: () => sheet && sheet.close() },
      ];
    present({ title: "Включить правило?", warn: true, body, actions });
  }

  function openEnableError(text) {
    present({
      title: "Включить правило?",
      body: h("p", { class: "rr-error", text }),
      actions: [{ text: "Вернуться к правилу", kind: "ghost", onClick: () => sheet && sheet.close() }],
    });
  }

  async function confirmEnable() {
    if (busy) return;
    busy = true;
    if (sheet) sheet.setBusy(true);
    try {
      const saved = await api(`/admin/reject-rules/${params.id}/enable`, {
        method: "POST",
        body: { confirm: true, base_rev: server.rev },
        quiet: true,
      });
      adopt(saved.rule);
      notice.say("Правило включено", "success");
      haptic("success");
    } catch (err) {
      if (err && err.status === 400) {
        openEnableError(errorText(err, ""));
        return;
      }
      if (err && err.status === 409) openStale(err.payload || {});
      else if (sheet) sheet.setError(errorText(err, SAVE_NET));
    } finally {
      busy = false;
    }
  }

  async function disableRule() {
    if (busy || isNew) return;
    if (dirty()) {
      notice.say("Сначала сохраните правки — потом выключим", "warn");
      return;
    }
    busy = true;
    try {
      const saved = await api(`/admin/reject-rules/${params.id}/disable`, {
        method: "POST",
        body: { base_rev: server.rev },
        quiet: true,
      });
      adopt(saved.rule);
      notice.say("Правило выключено", "success");
      haptic("success");
    } catch (err) {
      if (err && err.status === 409) openStale(err.payload || {});
      else notice.say(errorText(err, SAVE_NET), "danger");
    } finally {
      busy = false;
    }
  }

  function openCopy() {
    if (isNew) return;
    const rows = (catalog.cities || []).filter((item) => (item.code || null) !== (server.city || null));
    let picked = rows[0] ? (rows[0].code || null) : null;
    const host = h("div");
    function draw() {
      host.replaceChildren(...rows.map((item) => {
        const code = item.code || null;
        const own = !server.city && me && me.city && code === me.city;
        const on = code === picked;
        return h("button", {
          type: "button",
          class: "rr-check",
          role: "radio",
          "aria-checked": on ? "true" : "false",
          onClick: () => { picked = code; draw(); },
        }, h("span", { text: own ? "Скопировать себе" : (item.label || "") }));
      }));
    }
    draw();
    present({
      title: "Скопировать правило",
      body: host,
      actions: [{
        text: "Скопировать правило",
        kind: "primary",
        note: COPY_NOTE,
        onClick: () => sendCopy(picked),
      }],
    });
  }

  async function sendCopy(city) {
    if (busy) return;
    busy = true;
    if (sheet) sheet.setBusy(true);
    try {
      const copied = await api(`/admin/reject-rules/${params.id}/copy`, {
        method: "POST",
        body: { city },
        quiet: true,
      });
      putFlash("Копия создана выключенной");
      location.hash = `#/admin-reject-rule/${copied.rule.id}`;
    } catch (err) {
      if (sheet) {
        sheet.setBusy(false);
        sheet.setError(errorText(err, LOCKED));
      }
    } finally {
      busy = false;
    }
  }

  async function openDelete() {
    if (isNew || busy) return;
    busy = true;
    try {
      const result = await api(`/admin/reject-rules/${params.id}?confirm=0`, {
        method: "DELETE",
        quiet: true,
      });
      const ask = (result && result.needs_confirm) || {};
      const actions = [];
      if (ask.can_disable_instead) {
        actions.push({
          text: "Выключить вместо удаления",
          kind: "primary",
          onClick: () => { if (sheet) sheet.close(); disableRule(); },
        });
      }
      actions.push({ text: "Удалить навсегда", kind: "danger", onClick: () => sendDelete() });
      actions.push({ text: "Не удалять", kind: "ghost", onClick: () => sheet && sheet.close() });
      present({
        title: `Удалить правило «${draft.name || "Правило"}»?`,
        warn: true,
        body: h("p", { text: ask.text || "" }),
        actions,
      });
    } catch (err) {
      notice.say(errorText(err, SAVE_NET), "danger");
    } finally {
      busy = false;
    }
  }

  async function sendDelete() {
    if (busy) return;
    busy = true;
    if (sheet) sheet.setBusy(true);
    try {
      await api(`/admin/reject-rules/${params.id}?confirm=1`, { method: "DELETE", quiet: true });
      forget();
      notice.say("Правило удалено", "success");
      location.hash = "#/admin-reject-rules";
    } catch (err) {
      if (sheet) sheet.setError(errorText(err, SAVE_NET));
    } finally {
      busy = false;
    }
  }

  function askReset() {
    const box = confirmBox(h, {
      text: "Сбросить все несохранённые правки?",
      confirmText: "Сбросить правки",
      cancelText: "Продолжить правку",
      onConfirm: () => {
        draft = fromRule(server);
        forget();
        paint("");
      },
    });
    box.open();
    root.append(box);
  }

  function bindBack() {
    if (restoreBack) restoreBack();
    restoreBack = null;
    if (readOnly) return;
    restoreBack = suspendBackButton(() => {
      if (!dirty()) {
        if (restoreBack) restoreBack();
        restoreBack = null;
        history.back();
        return;
      }
      const box = confirmBox(h, {
        text: "Выйти без сохранения? Правки в этом правиле пропадут.",
        confirmText: "Выйти без сохранения",
        cancelText: "Продолжить правку",
        onConfirm: () => {
          if (restoreBack) restoreBack();
          restoreBack = null;
          history.back();
        },
      });
      box.open();
      root.append(box);
    });
  }

  function present(opts) {
    const prev = opts.onClose;
    sheet = openSheet(h, {
      ...opts,
      onClose() {
        sheet = null;
        if (prev) prev();
        if (alive) bindBack();
      },
    });
    return sheet;
  }

  function paintMissing() {
    root.replaceChildren(h("div", { class: "rr-screen" },
      h("h1", { class: "rr-title", text: "Это правило уже удалили" }),
      h("button", {
        type: "button", class: "btn", text: "К списку",
        onClick: () => { location.hash = "#/admin-reject-rules"; },
      }),
    ));
  }

  function paintLocked(text) {
    root.replaceChildren(h("div", { class: "rr-screen" },
      h("h1", { class: "rr-title", text: "Правило" }),
      h("p", { text: text || LOCKED }),
      h("button", {
        type: "button", class: "btn ghost", text: "К списку",
        onClick: () => { location.hash = "#/admin-reject-rules"; },
      }),
    ));
  }

  function paintLoadError() {
    root.replaceChildren(
      errorState(h, { me, text: "Не удалось открыть правило", retry: () => render(root, params, ctx) }),
      h("button", {
        type: "button", class: "btn ghost", text: "К списку правил",
        onClick: () => { location.hash = "#/admin-reject-rules"; },
      }),
    );
  }
}
