// Экран «🚀 Первая настройка» (квик 260915-4mu) — мастер ведёт менеджера по обязательным
// шагам первой настройки события, начиная с типа события (см. `miniapp/setup_wizard.py`
// и план квика для полного контекста решений владельца).
//
// Экран — ЧИСТЫЙ потребитель `form.js` (та же дисциплина, что `screens/settings.js`): ни
// одного собственного контрола по типу поля, ни одной надписи литералом — весь человеческий
// текст идёт из `payload.texts` (`GET /admin/setup`) и из полей ответа (label/help/hint/
// title). Запись — ТОЛЬКО существующий `POST /admin/settings/batch`, свой путь записи мастер
// не заводит (403/405 у `/admin/setup` на POST — сервер, не фронт).
//
// Своего состояния мастер не хранит (решение владельца, план п.6): источник правды —
// `bot_settings` через `GET /admin/setup` на каждый вход/после каждого сохранения. Индекс
// текущего шага — МОДУЛЬНАЯ переменная `currentKey` (тот же приём, что `pending` в
// `screens/settings.js`): переживает переход на `#/settings/form` по шагу-ссылке и обратно.

import { errorState, flatRow, labelText } from "../ui.js";
import { haptic } from "../motion.js";
import {
  field, setFieldState, settingSpec, confirmBox,
  errorText, isAuthError as isAuthErrorBase,
} from "../form.js";

const AUTH_EXCEPT_REASONS = ["not_editable"];
function isAuthError(err) {
  return isAuthErrorBase(err, AUTH_EXCEPT_REASONS);
}

// Значение контрола (массив/файл-id/строка) -> строка для POST settings/batch — тот же
// маленький чистый хелпер, что `screens/settings.js::toBatchValue` (не экспортирован из
// form.js, дублирование по той же дисциплине, что у соседних экранов).
function toBatchValue(v) {
  if (v == null) return null;
  if (Array.isArray(v)) return v.join(";");
  return String(v);
}

// Переживает переход на #/settings/form (шаг-ссылка) и обратно — докстринг файла выше.
let currentKey = null;

export async function render(root, params, ctx) {
  const { h, api, navigate, setMainButton, me } = ctx;

  let texts = {};
  let data = null;
  let busy = false;
  const pending = new Map(); // key -> значение с контрола ТЕКУЩЕГО шага (сбрасывается со сменой шага)
  const fieldEls = new Map(); // key -> узел field() текущего шага (для setFieldState при errors)
  let confirmedKeys = new Set(); // подтверждённые в этой попытке сохранения ключи (needs_confirm)

  const title = h("h1", {});
  const progressFill = h("div", { class: "wizard-progress-fill" });
  const progressBar = h("div", { class: "wizard-progress flush" }, progressFill);
  const progressNote = h("p", { class: "wizard-progress-note" });
  const progressRow = h("div", { class: "wizard-progress-row" }, progressBar, progressNote);
  const allStepsBtn = h("button", {
    class: "btn ghost", type: "button", onClick: () => stepList.classList.toggle("hidden"),
  });
  const stepList = h("div", { class: "flat-list hidden" });
  const stateWrap = h("div");
  const body = h("div", { class: "wizard-step" });
  const backBtn = h("button", { class: "btn ghost", type: "button", onClick: () => goBack() });
  const skipBtn = h("button", { class: "btn ghost", type: "button", onClick: () => goSkip() });
  const footerActions = h("div", { class: "task-actions hidden" }, backBtn, skipBtn);
  const hideTileBtn = h("button", { class: "btn ghost", type: "button", onClick: () => hideTile() });

  const stepConfirm = confirmBox(h, {
    onConfirm: () => { stepConfirm.close(); submitAndAdvance(); },
  });

  const toast = h("div", { class: "settings-toast chip off" }, h("span", {}));
  let toastTimer = null;
  function showToast(text) {
    if (!text) return;
    toast.querySelector("span").textContent = text;
    toast.classList.remove("off");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.add("off"), 2400);
  }

  root.append(title, progressRow, allStepsBtn, stepList, stateWrap, body, footerActions, hideTileBtn, stepConfirm, toast);

  function renderSkeleton() {
    const bars = Array.from({ length: 4 }, () => {
      const bar = h("div", { class: "card" });
      bar.style.height = "48px";
      bar.style.opacity = ".5";
      return bar;
    });
    stateWrap.replaceChildren(...bars);
  }

  function clearState() {
    stateWrap.replaceChildren();
  }

  function markerChip(step) {
    return h("span", {
      class: `chip field-state ${step.done ? "is-set" : "is-default"}`,
      text: step.done ? (texts.value_set_text || "") : (texts.value_default_text || ""),
    });
  }

  function renderStepList() {
    stepList.replaceChildren(...data.steps.map((step) => flatRow(h, {
      title: labelText(step.title),
      trailing: step.counts ? markerChip(step) : null,
      onClick: () => {
        stepList.classList.add("hidden");
        currentKey = step.key;
        haptic("light");
        drawStep();
      },
    })));
  }

  function renderProgress() {
    const pct = data.total ? Math.round((data.done_count / data.total) * 100) : 0;
    progressFill.style.width = `${pct}%`;
    progressNote.textContent = (texts.progress_note_text || "")
      .replace("{done}", String(data.done_count)).replace("{total}", String(data.total));
  }

  function currentStepIndex() {
    return data.steps.findIndex((s) => s.key === currentKey);
  }

  function currentMainLabel() {
    const idx = currentStepIndex();
    const isLast = idx === data.steps.length - 1;
    if (pending.size) return texts.save_next_text || "";
    return isLast ? (texts.done_text || "") : (texts.next_text || "");
  }

  function updateMainButton() {
    setMainButton(currentMainLabel(), goNext, { disabled: busy });
  }

  // ── загрузка фото/файла реестра — тот же путь, что `screens/settings.js::handleFileUpload`
  // (POST /app/api/uploads?target=settings_asset), только без специализированных текстов
  // ошибки (у мастера один общий error_toast_text — узкий каркас, не полноценный экран
  // настроек). ────────────────────────────────────────────────────────────────────────────
  let uploadLimitsPromise = null;
  function getUploadLimits() {
    if (!uploadLimitsPromise) uploadLimitsPromise = api("/uploads/limits").catch(() => ({}));
    return uploadLimitsPromise;
  }
  const IMAGE_EXT_RE = /\.(jpe?g|png|webp)$/i;

  async function handleFileUpload(item, el, file) {
    if (item.type === "photo" && !(file.type || "").startsWith("image/") && !IMAGE_EXT_RE.test(file.name || "")) {
      setFieldState(el, "error", { text: texts.error_toast_text || "" });
      return;
    }
    const limits = await getUploadLimits();
    if (limits.max_bytes && file.size > limits.max_bytes) {
      setFieldState(el, "error", { text: texts.error_toast_text || "" });
      return;
    }
    setFieldState(el, "uploading", { text: "" });
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const res = await api("/uploads?target=settings_asset", { method: "POST", form });
      pending.set(item.key, res.content);
      setFieldState(el, "default");
      updateMainButton();
    } catch (err) {
      if (isAuthError(err)) { setFieldState(el, "default"); return; }
      setFieldState(el, "error", { text: errorText(err, texts.error_toast_text || "") });
    }
  }

  function onFieldChange(item, el, value) {
    if ((item.type === "photo" || item.type === "file") && typeof File !== "undefined" && value instanceof File) {
      handleFileUpload(item, el, value);
      return;
    }
    pending.set(item.key, toBatchValue(value));
    updateMainButton();
  }

  function drawStep() {
    const idx = currentStepIndex();
    const step = data.steps[Math.max(idx, 0)];
    pending.clear();
    fieldEls.clear();
    confirmedKeys = new Set();
    body.replaceChildren();
    footerActions.classList.remove("hidden");
    backBtn.disabled = idx <= 0;
    backBtn.classList.toggle("hidden", idx <= 0);

    const stepOf = (texts.step_of_text || "")
      .replace("{n}", String(idx + 1)).replace("{m}", String(data.steps.length));
    body.append(
      h("p", { class: "label-role", text: stepOf }),
      h("h2", { text: step.title }),
    );
    if (step.hint) body.append(h("p", { class: "label-role", text: step.hint }));

    if (step.kind === "fields") {
      for (const item of step.fields) {
        const el = field(h, settingSpec(item), item.value, (v) => onFieldChange(item, el, v));
        fieldEls.set(item.key, el);
        body.append(el);
      }
    } else if (step.kind === "link" && step.link) {
      body.append(h("button", {
        class: "btn ghost", type: "button", text: step.link.label || "",
        onClick: () => navigate(step.link.hash),
      }));
    }
    // kind === "note" — карточка выше уже несёт весь текст (hint), полей нет.

    updateMainButton();
  }

  function advanceFrom(savedKey) {
    const idx = data.steps.findIndex((s) => s.key === savedKey);
    const next = idx >= 0 ? data.steps[idx + 1] : data.steps[0];
    if (!next) { navigate("#/hub"); return; }
    currentKey = next.key;
    haptic("light");
    drawStep();
  }

  function goSkip() {
    const idx = currentStepIndex();
    const next = data.steps[idx + 1];
    if (!next) { navigate("#/hub"); return; }
    currentKey = next.key;
    haptic("light");
    drawStep();
  }

  function goBack() {
    const idx = currentStepIndex();
    if (idx <= 0) return;
    currentKey = data.steps[idx - 1].key;
    haptic("light");
    drawStep();
  }

  async function refreshData() {
    const resp = await api("/admin/setup");
    data = resp;
    texts = data.texts || {};
    renderProgress();
    renderStepList();
  }

  // Возвращает true при успешном сохранении (после него `data`/`texts` уже свежие).
  async function saveStep() {
    busy = true;
    updateMainButton();
    try {
      const changes = [...pending.entries()].map(([key, value]) => ({ key, value }));
      const base = {};
      const step = data.steps[currentStepIndex()];
      for (const item of step.fields) base[item.key] = item.raw ?? null;
      const resp = await api("/admin/settings/batch", {
        method: "POST",
        body: { changes, base, confirm: [...confirmedKeys] },
      });

      const errs = resp.errors || {};
      if (Object.keys(errs).length) {
        for (const [key, text] of Object.entries(errs)) {
          const el = fieldEls.get(key);
          if (el) setFieldState(el, "error", { text });
        }
        return false;
      }
      const needsConfirm = resp.needs_confirm || [];
      if (needsConfirm.length) {
        confirmedKeys = new Set([...confirmedKeys, ...needsConfirm.map((entry) => entry.key)]);
        stepConfirm.querySelector("p").textContent = needsConfirm[0].text || "";
        stepConfirm.querySelector(".btn.danger").textContent = texts.save_next_text || "";
        stepConfirm.querySelector(".btn.ghost").textContent = texts.skip_text || "";
        stepConfirm.open();
        return false;
      }
      if ((resp.stale || []).length) {
        await refreshData();
        drawStep();
        return false;
      }
      pending.clear();
      await refreshData();
      return true;
    } catch (err) {
      if (!isAuthError(err)) showToast(errorText(err, texts.error_toast_text || ""));
      return false;
    } finally {
      busy = false;
      updateMainButton();
    }
  }

  async function submitAndAdvance() {
    if (busy) return;
    const savedKey = currentKey;
    const ok = await saveStep();
    if (!ok) return;
    advanceFrom(savedKey);
  }

  async function goNext() {
    if (busy) return;
    if (pending.size) {
      await submitAndAdvance();
      return;
    }
    const idx = currentStepIndex();
    const next = data.steps[idx + 1];
    if (!next) { navigate("#/hub"); return; }
    currentKey = next.key;
    haptic("light");
    drawStep();
  }

  async function hideTile() {
    try {
      await api("/admin/settings/batch", {
        method: "POST",
        body: { changes: [{ key: "setup_wizard_dismissed", value: "on" }], base: {}, confirm: [] },
      });
    } catch (err) {
      if (!isAuthError(err)) showToast(errorText(err, texts.error_toast_text || ""));
      return;
    }
    navigate("#/hub");
  }

  async function loadAndRender() {
    renderSkeleton();
    body.replaceChildren();
    footerActions.classList.add("hidden");
    let resp;
    try {
      resp = await api("/admin/setup");
    } catch (err) {
      clearState();
      if (isAuthError(err)) return;
      stateWrap.append(errorState(h, {
        me, text: errorText(err, texts.error_toast_text || ""), retry: loadAndRender,
      }));
      return;
    }
    data = resp;
    texts = data.texts || {};
    clearState();

    title.textContent = texts.title || "";
    allStepsBtn.textContent = texts.all_steps_text || "";
    backBtn.textContent = texts.back_text || "";
    skipBtn.textContent = texts.skip_text || "";
    hideTileBtn.textContent = texts.hide_tile_text || "";

    if (!data.steps.length) return;
    if (!currentKey || !data.steps.some((s) => s.key === currentKey)) currentKey = data.steps[0].key;

    renderProgress();
    renderStepList();
    drawStep();
  }

  await loadAndRender();
}
