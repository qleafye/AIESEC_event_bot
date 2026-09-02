// Экран анкеты Mini App `#/form` (план 21-11, FORM-SYNC-05, D-24/D-26): один файл, два режима
// — пошаговый мастер (kind='new', новая анкета/продолжение черновика) и обзор точечной правки
// уже поданной анкеты (kind='edit', approved/pending/rejected). Экран не знает полей и правил
// анкеты — он рисует то, что вернул GET /app/api/reg/draft, и отправляет обратно тем же
// контрактом (PATCH), что и общая логика form.js. Подписи — из ответа сервера (реестр
// reg_form_*), человеческих текстовых литералов здесь нет (D-25), как в form.js.
//
// activated (Telegram SDK): при возврате фокуса приложения экран перечитывает черновик и
// подмешивает чужие правки (createFormState().applyServer(..., {keepDirty:true})) — своя
// правка, которую человек прямо сейчас вводит, не перетирается (D-19).

import {
  field, setFieldState, createFormState, diffView, confirmBox, errorText,
  isAuthError as isAuthErrorBase,
} from "../form.js";
import { flatRow } from "../ui.js";
import { icon } from "../icons.js";
import { haptic } from "../motion.js";

const AUTH_EXCEPT_REASONS = [];
function isAuthError(err) {
  return isAuthErrorBase(err, AUTH_EXCEPT_REASONS);
}

// activated-подписка живёт на модуле (не на экземпляре render()), чтобы unmount() могла её
// снять, даже если render() ещё не успел отрисовать первый кадр (T-19.1-14-подобный паттерн).
let activatedHandler = null;
let tgRef = null;

function buildFormState(draft) {
  const specs = (draft.steps || []).map((s) => ({ ...s }));
  const values = {};
  for (const s of specs) values[s.column] = s.value;
  return createFormState(specs, values);
}

function answersFromSteps(steps) {
  const out = {};
  for (const s of steps || []) out[s.column] = s.value;
  return out;
}

function stepIndexFromKey(specs, stepKey) {
  if (!stepKey) return 0;
  const idx = specs.findIndex((s) => s.key === stepKey);
  return idx >= 0 ? idx : 0;
}

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton, tg } = ctx;
  tgRef = tg;

  const notice = h("p", { class: "chip accent hidden" });
  const holder = h("div");
  root.append(notice, holder);

  function say(text, kind) {
    notice.textContent = text || "";
    notice.className = `chip ${kind || "accent"}${text ? "" : " hidden"}`;
  }

  function goHome() {
    navigate("#/hub");
  }

  // D-17: deep-link — только с сервера (`d.continue_deeplink`, тот же приём, что
  // profile.js/`me.edit_deeplink`) — фронт не собирает `t.me/...` строкой сам.
  function continueInChat(deeplink) {
    if (tg && typeof tg.openTelegramLink === "function" && deeplink) {
      tg.openTelegramLink(deeplink);
      if (typeof tg.close === "function") tg.close();
    }
  }

  function chatLink(text, deeplink) {
    return h("button", { class: "btn ghost", type: "button", onClick: () => continueInChat(deeplink) },
      icon("message-circle"), h("span", { text: text || "" }));
  }

  // ── activated: подхват чужих правок из чата (D-19) — регистрируется ДО первого await,
  // чтобы unmount() могла снять обработчик, даже если запрос черновика ещё не вернулся. ────
  let onRefresh = null; // выставляется renderWizard()/renderOverview() ниже
  function onActivated() {
    if (onRefresh) onRefresh();
  }
  if (tg && typeof tg.onEvent === "function") {
    tg.onEvent("activated", onActivated);
    activatedHandler = onActivated;
  }

  function renderComplete(res) {
    onRefresh = null;
    setMainButton(null);
    holder.replaceChildren(
      h("section", { class: "state" },
        h("div", { class: "icon" }, icon("check")),
        h("h1", { text: res.heading || "" }),
        res.body ? h("p", { text: res.body }) : null,
        h("div", { class: "actions" },
          h("button", { class: "btn", type: "button", onClick: goHome }, icon("check")),
        ),
      ),
    );
  }

  let draft;
  try {
    draft = await api("/reg/draft");
  } catch (err) {
    if (!isAuthError(err)) holder.replaceChildren(h("p", { class: "error-inline", text: errorText(err, "") }));
    return;
  }

  if (draft.closed) {
    onRefresh = null;
    setMainButton(null);
    holder.replaceChildren(h("section", { class: "state" },
      h("div", { class: "icon" }, icon("clock")),
      h("p", { text: draft.closed_text || "" }),
    ));
    return;
  }

  if (draft.kind === "edit") await renderOverview(draft);
  else await renderWizard(draft);

  // ════════════════════════════════════════════════════════════════════════════════════
  // Режим «обзор правки» (kind='edit', D-26, Task 2): плоский список ответов, точечная
  // правка по тапу, «Отменить изменения» / «Отправить изменения» — только при dirty≥1.
  // ════════════════════════════════════════════════════════════════════════════════════
  async function renderOverview(initialDraft) {
    let d = initialDraft;
    let state = buildFormState(d);
    const fieldEls = {};
    let busy = false;

    onRefresh = async () => {
      try {
        const fresh = await api("/reg/draft");
        d = fresh;
        const touched = state.applyServer(answersFromSteps(fresh.steps), { keepDirty: true });
        for (const column of touched) {
          const el = fieldEls[column];
          if (el) setFieldState(el, "updated-in-chat", { text: fresh.updated_in_chat_badge_text });
        }
        drawList();
      } catch (_) { /* фоновая проверка — экран не падает */ }
    };

    const cancelBox = confirmBox(h, {
      text: d.cancel_changes_confirm_text,
      confirmText: d.cancel_changes_text,
      cancelText: d.continue_in_chat_text,
      onConfirm: async () => {
        try {
          d = await api("/reg/draft");
          state = buildFormState(d);
          cancelBox.close();
          drawList();
        } catch (err) {
          if (!isAuthError(err)) say(errorText(err, ""), "warn");
        }
      },
      onCancel: () => {},
    });

    async function submitChanges() {
      if (busy) return;
      busy = true;
      drawList();
      try {
        const res = await api("/reg/draft/submit", { method: "POST" });
        haptic("success");
        renderComplete(res);
      } catch (err) {
        busy = false;
        if (!isAuthError(err)) say(errorText(err, ""), "warn");
        drawList();
      }
    }

    function fieldRow(spec) {
      const column = spec.column;
      const locked = Boolean(spec.locked);
      const value = state.value(column);
      const displayValue = value == null || value === "" ? (d.not_set_text || "") : String(value);
      if (locked) {
        return flatRow(h, { title: spec.label, meta: displayValue });
      }
      const panel = h("div", { class: "field hidden" });
      let liveValue = value;
      function open() {
        const wasHidden = panel.classList.contains("hidden");
        panel.classList.add("hidden");
        panel.replaceChildren();
        if (!wasHidden) return;
        const el = field(h, spec, value, (v) => { liveValue = v; });
        fieldEls[column] = el;
        panel.append(el, h("button", {
          class: "btn", type: "button", "aria-label": spec.label,
          onClick: () => {
            state.setValue(column, liveValue);
            drawList();
          },
        }, icon("check")));
        panel.classList.remove("hidden");
      }
      const row = flatRow(h, { title: spec.label, meta: displayValue, trailing: icon("pen-line"), onClick: open });
      return h("div", {}, row, panel);
    }

    function drawList() {
      const list = h("div", { class: "flat-list" }, ...state.specs.map(fieldRow));
      const diffBox = diffView(h, state.base, state.current, { wasPrefix: "" });
      const dirty = state.specs.some((s) => state.isDirty(s.column));
      const footer = dirty
        ? h("div", { class: "task-actions" },
          h("button", { class: "btn ghost", type: "button", onClick: () => cancelBox.open() },
            icon("undo-2"), h("span", { text: d.cancel_changes_text || "" })),
          cancelBox,
          h("button", { class: "btn", type: "button", disabled: busy, onClick: submitChanges },
            h("span", { text: d.submit_cta_text || "" })),
        )
        : null;
      const banner = d.rejected_banner_text
        ? h("div", { class: "confirm-box" }, h("p", { text: d.rejected_banner_text }))
        : null;
      holder.replaceChildren(...[banner, list, diffBox, footer].filter(Boolean));
      setMainButton(dirty ? (d.submit_cta_text || null) : null, dirty ? submitChanges : null, { disabled: busy });
    }

    drawList();
  }

  // ════════════════════════════════════════════════════════════════════════════════════
  // Режим «мастер» (kind='new', D-03/D-04): pre-flow (согласия → развилки город/формат) →
  // шаги по одному → submit.
  // ════════════════════════════════════════════════════════════════════════════════════
  async function renderWizard(initialDraft) {
    let d = initialDraft;
    let state = buildFormState(d);
    let stepIndex = stepIndexFromKey(state.specs, d.step);
    let preIndex = 0;
    let busy = false;
    const signedConsents = new Set();

    onRefresh = async () => {
      try {
        const fresh = await api("/reg/draft");
        d = fresh;
        const touched = state.applyServer(answersFromSteps(fresh.steps), { keepDirty: true });
        if (touched.length) say(fresh.conflict_text || "", "accent");
        drawCurrent();
      } catch (_) { /* фоновая проверка — экран не падает */ }
    };

    // ── pre-flow: список экранов строится из d.pre_items при КАЖДОМ drawCurrent(): первый
    // экран — все согласия (D-23/A1), затем по экрану на каждую развилку (элемент с `field`).
    // После успешного PATCH развилки сервер её больше не отдаёт — preScreens укорачивается,
    // preIndex не трогаем: следующий экран подтягивается сам. Экран не знает имён полей и
    // типов развилок — только item.field / item.text / item.options / item.value.
    function buildPreScreens(items) {
      const consentItems = items.filter((it) => it.type === "consent");
      const infoItems = items.filter((it) => it.type !== "consent" && !it.field);
      const preScreens = [];
      if (consentItems.length || infoItems.length) preScreens.push({ consents: consentItems, info: infoItems });
      for (const item of items) {
        if (item.field) preScreens.push({ fork: item });
      }
      return preScreens;
    }

    function drawCurrent() {
      const preScreens = buildPreScreens(d.pre_items || []);
      if (preIndex < preScreens.length) {
        const screen = preScreens[preIndex];
        if (screen.fork) drawFork(screen.fork);
        else drawPre(screen.consents, screen.info);
      } else {
        drawStep();
      }
    }

    // Общая ветка «регистрация закрыта» для PATCH из pre-flow и из шага (D-11).
    function showClosed(err) {
      onRefresh = null;
      setMainButton(null);
      holder.replaceChildren(h("section", { class: "state" },
        h("p", { text: (err.payload && err.payload.text) || "" })));
    }

    // Пересборка состояния целиком: выбор трека меняет список шагов, applyServer тут не годится.
    function adoptDraft(res) {
      d = res;
      state = buildFormState(res);
      stepIndex = stepIndexFromKey(state.specs, res.step);
    }

    // ── pre-flow: развилка (город / формат участия) — пикер choice-chips из item.options ──
    function drawFork(item) {
      const options = item.options || [];
      const current = options.find((o) => o.code === item.value);
      let chosen = current ? current.label : null;
      const el = field(h, {
        key: item.type, type: "choice-chips", label: item.text || "",
        options: options.map((o) => o.label),
      }, chosen, (v) => { chosen = v; });
      const errorZone = el._nodes && el._nodes.errorZone;
      function showError(msg) {
        if (errorZone) { errorZone.textContent = msg || ""; errorZone.classList.remove("hidden"); }
      }

      async function next() {
        if (busy) return;
        busy = true;
        const picked = options.find((o) => o.label === chosen);
        // Без выбора уходит пустая строка — текст ошибки вернёт сервер (литерала здесь нет).
        const body = { version: d.version };
        body[item.field] = picked ? picked.code : "";
        try {
          const res = await api("/reg/draft", { method: "PATCH", body });
          busy = false;
          adoptDraft(res);
          drawCurrent();
        } catch (err) {
          busy = false;
          if (err && err.status === 400 && err.reason === "invalid" && err.payload && err.payload.errors) {
            showError(err.payload.errors[item.field] || errorText(err, ""));
          } else if (err && err.status === 409 && err.reason === "already_set") {
            // Значение уже зафиксировано в чате — перечитать черновик, экран исчезнет сам.
            try { adoptDraft(await api("/reg/draft")); } catch (_) { /* фон — экран не падает */ }
            drawCurrent();
          } else if (err && err.status === 403 && err.reason === "registration_closed") {
            showClosed(err);
          } else if (!isAuthError(err)) {
            showError(errorText(err, ""));
          }
        }
      }

      holder.replaceChildren(
        h("div", { class: "wizard-step" },
          el,
          h("div", { class: "task-actions" },
            h("button", { class: "btn", type: "button", disabled: busy, "aria-label": item.text || "", onClick: next }, icon("check")),
            chatLink(d.continue_in_chat_text, d.continue_deeplink),
          ),
        ),
      );
      setMainButton(null);
    }

    // ── pre-flow: согласия одним прокручиваемым списком (D-23) + информационные карточки ─
    function drawPre(consentItems, otherItems) {
      const cards = [];

      for (const item of consentItems) {
        const cb = h("input", { type: "checkbox" });
        cb.checked = signedConsents.has(item.key);
        cb.addEventListener("change", () => {
          if (cb.checked) signedConsents.add(item.key);
          else signedConsents.delete(item.key);
        });
        const card = h("div", { class: "consent-card" },
          icon("shield-check"),
          h("label", { class: "check" }, cb, h("span", { text: item.label || "" })),
        );
        if (item.pdf_file_id) {
          card.append(h("a", {
            class: "btn ghost", target: "_blank", "aria-label": item.label,
            href: `/app/api/file/${encodeURIComponent(item.pdf_file_id)}`,
          }, icon("file-text")));
        }
        cards.push(card);
      }
      for (const item of otherItems) {
        cards.push(h("div", { class: "consent-card" }, h("p", { text: item.text || "" })));
      }

      const errorBox = h("p", { class: "field-error hidden", "aria-live": "polite" });

      async function next() {
        if (busy) return;
        const missing = consentItems.filter((it) => !signedConsents.has(it.key));
        if (missing.length) {
          errorBox.textContent = d.consent_required_text || "";
          errorBox.classList.remove("hidden");
          return;
        }
        busy = true;
        try {
          for (const item of consentItems) {
            await api(`/reg/consent/${encodeURIComponent(item.key)}`, { method: "POST" });
          }
          preIndex += 1;
          busy = false;
          drawCurrent();
        } catch (err) {
          busy = false;
          if (!isAuthError(err)) {
            errorBox.textContent = errorText(err, "");
            errorBox.classList.remove("hidden");
          }
        }
      }

      holder.replaceChildren(
        h("div", { class: "wizard-step" },
          ...cards, errorBox,
          h("div", { class: "task-actions" },
            h("button", { class: "btn", type: "button", disabled: busy, onClick: next }, icon("check")),
            chatLink(d.continue_in_chat_text, d.continue_deeplink),
          ),
        ),
      );
      setMainButton(null);
    }

    // ── шаги анкеты: один вопрос на экран (D-03) ────────────────────────────────────────
    function drawStep() {
      const specs = state.specs;
      if (!specs.length || stepIndex >= specs.length) { submitForm(); return; }
      const spec = specs[stepIndex];
      const column = spec.column;
      const value = state.value(column);

      let liveValue = value;
      // Badge «из прошлой анкеты» (D-07) переиспользует визуал badge/refresh-cw поля
      // (form.js::field() строит один badge-узел на состояние «updated-in-chat» — второго
      // набора DOM-узлов под отдельную иконку «history» общий компонент сегодня не даёт);
      // подпись — параметром из ответа сервера ниже, не литерал. Снимается первым касанием.
      const el = field(h, spec, value, (v) => { liveValue = v; });
      if (spec.value_source === "prior" && !state.isDirty(column)) {
        setFieldState(el, "updated-in-chat", { text: d.prior_badge_text });
        el.addEventListener("input", () => setFieldState(el, "default", {}), { once: true });
        el.addEventListener("change", () => setFieldState(el, "default", {}), { once: true });
      }

      const errorZone = el._nodes && el._nodes.errorZone;

      async function goNext() {
        if (busy) return;
        busy = true;
        setMainButton(null);
        state.setValue(column, liveValue);
        const patch = {};
        patch[column] = liveValue;
        try {
          const res = await api("/reg/draft", {
            method: "PATCH", body: { version: d.version, answers: patch, step: spec.key },
          });
          d = res;
          state.applyServer(answersFromSteps(res.steps), { keepDirty: false });
          busy = false;
          stepIndex += 1;
          drawStep();
        } catch (err) {
          busy = false;
          if (err && err.status === 400 && err.reason === "invalid" && err.payload && err.payload.errors) {
            const msg = err.payload.errors[column];
            if (errorZone && msg) { errorZone.textContent = msg; errorZone.classList.remove("hidden"); }
            setMainButton(null, null);
            setMainButton("→", goNext);
          } else if (err && err.status === 403 && err.reason === "registration_closed") {
            showClosed(err);
          } else if (!isAuthError(err)) {
            if (errorZone) { errorZone.textContent = errorText(err, ""); errorZone.classList.remove("hidden"); }
            setMainButton("→", goNext);
          }
        }
      }

      function goBack() {
        if (busy) return;
        stepIndex = Math.max(0, stepIndex - 1);
        drawStep();
      }

      const showProgress = Boolean(d.show_progress) && specs.length > 0;
      const eyebrow = showProgress
        ? h("div", {},
          h("p", { class: "label-role", "aria-hidden": "true", text: `${stepIndex + 1}/${specs.length}` }),
          h("div", {
            class: "wizard-progress", role: "progressbar",
            "aria-valuenow": String(stepIndex + 1), "aria-valuemin": "1", "aria-valuemax": String(specs.length),
            "aria-label": `${stepIndex + 1}/${specs.length}`,
          },
            h("div", { class: "wizard-progress-fill", style: `width:${Math.round(((stepIndex + 1) / specs.length) * 100)}%` })),
        )
        : null;

      const footer = h("div", { class: "task-actions" },
        stepIndex > 0 ? h("button", { class: "btn ghost", type: "button", "aria-label": spec.label, onClick: goBack }, icon("chevron-right")) : null,
        h("button", { class: "btn", type: "button", disabled: busy, "aria-label": spec.label, onClick: goNext }, icon("chevron-right")),
      );

      holder.replaceChildren(h("div", { class: "wizard-step" }, eyebrow, el, footer, chatLink(d.continue_in_chat_text, d.continue_deeplink)));
      setMainButton("→", goNext, { disabled: busy });
    }

    async function submitForm() {
      if (busy) return;
      busy = true;
      try {
        const res = await api("/reg/draft/submit", { method: "POST" });
        haptic("success");
        renderComplete(res);
      } catch (err) {
        busy = false;
        if (err && err.status === 409 && err.reason === "consent_required") {
          preIndex = 0;
          say(errorText(err, ""), "warn");
          drawCurrent();
        } else if (!isAuthError(err)) {
          say(errorText(err, ""), "warn");
          stepIndex = Math.max(0, state.specs.length - 1);
          drawStep();
        }
      }
    }

    drawCurrent();
  }
}

export function unmount() {
  if (tgRef && activatedHandler && typeof tgRef.offEvent === "function") {
    tgRef.offEvent("activated", activatedHandler);
  }
  activatedHandler = null;
  tgRef = null;
}
