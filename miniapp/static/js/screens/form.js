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
  isAuthError as isAuthErrorBase, stepIndexFromKey, validationErrors, firstFieldError,
} from "../form.js";
import { fileUrl, flatRow, sectionTitle, labelText } from "../ui.js";
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

// УАТ 10-11.09 (квик 260911-2kb, пункт 4): состояние формы заводит ВСЕ колонки шага
// (`s.values`, набор — `s.columns`), не только основную `s.column` — иначе `state.value()`
// не знает про `resume_file_id`/`resume_file_name` (резюме файлом), и `applyServer` после
// загрузки файла их не подхватывает. Сервер, который ещё не обновлён (`s.values` нет), даёт
// прежнее поведение байт-в-байт — второй веткой ниже.
function buildFormState(draft) {
  const specs = (draft.steps || []).map((s) => ({ ...s }));
  const values = {};
  for (const s of specs) {
    if (s.values) Object.assign(values, s.values);
    else values[s.column] = s.value;
  }
  return createFormState(specs, values);
}

function answersFromSteps(steps) {
  const out = {};
  for (const s of steps || []) {
    if (s.values) Object.assign(out, s.values);
    else out[s.column] = s.value;
  }
  return out;
}

// Один хелпер «шаг заполнен» на мастер и на обзор правки (пункт 4): заполнен, если непусто
// ЛЮБОЕ из `spec.columns` — не только основная колонка (резюме файлом уходит в
// resume_file_id/resume_file_name, а не в spec.column). Имён колонок анкеты здесь нет — только
// `spec.columns`, набор приходит с сервера.
function stepAnswered(spec, state) {
  return (spec.columns || [spec.column]).some((col) => {
    const v = state.value(col);
    return v != null && v !== "";
  });
}

// D13 (quick 260904-de4): «Поделиться номером» — доступно, только когда клиент физически
// умеет (Bot API 6.9+, requestContact есть); вынесено в одну функцию, используют оба режима
// (мастер и обзор правки).
function canShareContact(tg) {
  return Boolean(
    tg && typeof tg.requestContact === "function" && typeof tg.isVersionAtLeast === "function"
    && tg.isVersionAtLeast("6.9"),
  );
}

// Колбэк `requestContact()` у клиентов разной формы — разбираем защитно: успех = явный
// `status === "sent"` ИЛИ старая форма (нет `res`, `ok === true`); номер достаём только из
// `responseUnsafe.contact` — в старой форме колбэка его нет, тогда просто ничего не делаем
// (ручной ввод остаётся). Номер без «+», состоящий из цифр, нормализуем добавлением «+»
// (валидатор `phone` принимает и то, и то — человеку показываем канонический вид).
function shareContactButton(h, spec, el, text, onNumber) {
  if (spec.type !== "phone" || !canShareContact(tgRef)) return null;
  return h("button", {
    class: "btn secondary", type: "button", "aria-label": spec.label,
    onClick: () => {
      tgRef.requestContact((ok, res) => {
        const success = (res && res.status === "sent") || (!res && ok === true);
        if (!success) return;
        const raw = res && res.responseUnsafe && res.responseUnsafe.contact
          && res.responseUnsafe.contact.phone_number;
        if (!raw) return;
        const normalized = /^[0-9]+$/.test(raw) ? `+${raw}` : raw;
        const input = el.querySelector("input");
        if (input) input.value = normalized;
        onNumber(normalized);
      });
    },
  }, icon("smartphone"), h("span", { text: text || "" }));
}

// Строка списка вопросов анкеты (обзор 19.1 находка №6): общий помощник для окна вопросов
// мастера (renderWizard::drawStep) и списка обзора точечной правки (renderOverview::fieldRow,
// Task 3) — отвеченный вопрос показывает галку и значение основным цветом, неотвеченный —
// тусклый кружок и `not_set_text`. `opts.value` перекрывает `spec.value` там, где актуальнее
// живое значение состояния (обзор правки — `state.value(column)`, не значение с момента
// загрузки черновика). `opts.extraCls` — дополнительный модификатор строки (например
// `q-required` для незаполненного обязательного поля в обзоре правки).
function questionRow(h, spec, opts = {}) {
  const { onEdit, notSetText, value: valueOverride, extraCls } = opts;
  const raw = valueOverride !== undefined ? valueOverride : spec.value;
  // УАТ 10-11.09 (пункт 4): spec.display — уже существующий контракт «подпись сохранённого
  // файла» (form.js::fileControl) — резюме файлом отвечено, а `raw` (spec.value/opts.value)
  // пуст (файл лежит не в основной колонке). Показываем display, когда он есть, иначе
  // прежнее значение.
  const hasDisplay = spec.display != null && spec.display !== "";
  const answered = (raw != null && raw !== "") || hasDisplay;
  const shown = hasDisplay ? spec.display : raw;
  return flatRow(h, {
    icon: answered ? "check" : "circle",
    // D-04: строка уже с Lucide-иконкой слева (check/circle) — labelText (ui.js) снимает
    // ведущий эмодзи подписи вопроса REG_LABELS, не дублируя его рядом с иконкой (23.1-07).
    title: labelText(spec.label),
    value: answered ? String(shown) : (notSetText || ""),
    valueCls: answered ? "strong" : null,
    cls: [answered ? "q-done" : "q-empty", extraCls].filter(Boolean).join(" "),
    trailing: onEdit ? icon("pen-line") : null,
    onClick: onEdit || null,
  });
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

  // Quick 260904-3vm (эстафета): плита «анкета сейчас в чате» — общая точка для начального
  // рендера (draft.handoff), для 409 held_by_bot на любом PATCH (мастер/обзор) и для activated
  // (onRefresh увидел fresh.handoff). «Продолжить в чате» здесь — НЕ обычный chatLink: перед
  // открытием чата приложение обязано отдать владение (POST /reg/draft/release), иначе гвард
  // бота (handlers/reg_handoff.py) отобьёт делегата же его собственным вводом.
  function showHandoff(handoff) {
    onRefresh = null;
    setMainButton(null);
    const continueBtn = h("button", { class: "btn ghost", type: "button", onClick: async () => {
      try { await api("/reg/draft/release", { method: "POST" }); } catch (_) { /* fail-soft: чат всё равно откроется */ }
      continueInChat(handoff.deeplink);
    } }, icon("message-circle"), h("span", { text: handoff.continue_text || "" }));
    const takeoverBtn = h("button", { class: "btn", type: "button", onClick: async () => {
      try {
        const res = await api("/reg/draft/takeover", { method: "POST" });
        if (res.kind === "edit") await renderOverview(res);
        else await renderWizard(res);
      } catch (err) {
        if (!isAuthError(err)) say(errorText(err, ""), "warn");
      }
    } }, icon("smartphone"), h("span", { text: handoff.takeover_text || "" }));
    holder.replaceChildren(h("section", { class: "state" },
      h("div", { class: "icon" }, icon("message-circle")),
      h("p", { text: handoff.text || "" }),
      h("div", { class: "actions" }, continueBtn, takeoverBtn),
    ));
  }

  // D9 (quick 260904-de4): загрузка резюме файлом — общая для мастера и обзора правки.
  // `fileControl` (form.js) отдаёт наверх сырой `File`, сам ничего не грузит (Reuse Contract) —
  // эта функция звонит `POST /uploads?target=resume` (submissions.py::_upload_resume) и кладёт
  // результат в состояние формы через `markServerDirty` (файл уже применён сервером — повторный
  // PATCH текстом затёр бы его, см. `form.js::createFormState`).
  // `ctx.getDraft`/`ctx.setDraft` — доступ к переменной `d`, объявленной `let` в замыкании
  // каждого режима (мастер/обзор), эта функция режимов не знает.
  async function uploadResume(file, el, ctx) {
    let current = ctx.getDraft();
    if (!current.exists) {
      // Черновика у одобренного делегата, правящего анкету впервые с файла, может не быть —
      // `_upload_resume` без черновика отвечает 404 no_draft. Пустой PATCH его создаёт.
      try {
        current = await api("/reg/draft", { method: "PATCH", body: { version: current.version, answers: {} } });
        ctx.setDraft(current);
      } catch (err) {
        if (!isAuthError(err)) setFieldState(el, "error", { text: errorText(err, current.resume_upload_error_text) });
        return;
      }
    }
    setFieldState(el, "uploading", { text: "" });
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      await api("/uploads?target=resume", { method: "POST", form });
      const fresh = await api("/reg/draft");
      ctx.setDraft(fresh);
      ctx.state.applyServer(answersFromSteps(fresh.steps), { keepDirty: true });
      ctx.state.markServerDirty(ctx.column);
      setFieldState(el, "default");
      if (ctx.onDone) ctx.onDone(fresh);
    } catch (err) {
      if (isAuthError(err)) return;
      setFieldState(el, "error", { text: errorText(err, ctx.getDraft().resume_upload_error_text) });
    }
  }

  // Квик 260912-l53 (задача 2): «×» на дропзоне резюме — симметрично uploadResume выше: файл
  // загружается СРАЗУ по выбору (D9), значит и удаление обязано уезжать сразу, а не ждать
  // «Дальше» — обязательный шаг «resume» с пустым ответом поймал бы 400 invalid
  // (validate_answer на пустой строке), ленивая отправка до сервера в принципе не доедет.
  // `ctx.stepKey` — ключ ШАГА (не колонки, D-01 квика): сервер сам разворачивает набор
  // колонок через `reg_engine.columns_for_step`. Ранней ветки «черновика нет — выходим» НЕТ
  // НАМЕРЕННО: именно этот PATCH создаёт строку `reg_drafts` одобренному делегату, правящему
  // анкету впервые с удаления резюме — без него файл остался бы висеть только в `users`.
  async function removeResume(el, ctx) {
    const current = ctx.getDraft();
    setFieldState(el, "uploading", { text: "" });
    try {
      const fresh = await api("/reg/draft", {
        method: "PATCH",
        body: { version: current.version, answers: {}, clear: [ctx.stepKey] },
      });
      ctx.setDraft(fresh);
      ctx.state.applyServer(answersFromSteps(fresh.steps), { keepDirty: true });
      ctx.state.markServerDirty(ctx.column);
      setFieldState(el, "default");
      if (ctx.onDone) ctx.onDone(fresh);
    } catch (err) {
      if (isAuthError(err)) return;
      setFieldState(el, "error", { text: errorText(err, ctx.getDraft().resume_upload_error_text) });
    }
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
    // Phase 28 (28-06, SU-07, D-09): блок-предложение реф-ссылки — доп. узел ПОД стандартным
    // сообщением, только если сервер прислал `res.ambassador` (mode == "new" И тумблер
    // reg_offer_ref_link включён). Тумблер выключен -> ключа нет вовсе -> слот остаётся
    // пустым, экран идентичен контракту фазы 21 (28-UI-SPEC.md §6, стадия A/выключено).
    const ambassadorSlot = h("div");
    holder.replaceChildren(
      h("section", { class: "state" },
        h("div", { class: "icon" }, icon("check")),
        h("h1", { text: res.heading || "" }),
        res.body ? h("p", { text: res.body }) : null,
        h("div", { class: "actions" },
          h("button", { class: "btn", type: "button", onClick: goHome }, icon("check")),
        ),
        ambassadorSlot,
      ),
    );
    if (res.ambassador) renderAmbassadorOffer(ambassadorSlot, res.ambassador);
  }

  // Стадия A (28-UI-SPEC.md §6): заголовок/тело + «Хочу свою ссылку» (primary accent) /
  // «Позже» (ghost) в один ряд. «Позже» — блок просто исчезает, остаётся стандартный экран
  // (SU-07 «Позже -> просто завершение»); никакой записи в БД для этой ветки нет.
  function renderAmbassadorOffer(slot, amb) {
    slot.replaceChildren(
      h("div", { class: "ambassador-offer" },
        h("h2", { text: amb.heading || "" }),
        amb.body ? h("p", { text: amb.body }) : null,
        h("div", { class: "actions" },
          h("button", { class: "btn", type: "button", text: amb.cta || "", onClick: () => wantRefLink(slot) }),
          h("button", { class: "btn ghost", type: "button", text: amb.later || "", onClick: () => slot.replaceChildren() }),
        ),
      ),
    );
  }

  // Тап «Хочу свою ссылку» — POST /app/api/reg/ambassador (ставит is_ambassador=1 на
  // сервере), ТОТ ЖЕ экран переходит в стадию B (без перехода на новый маршрут, A2 UI-SPEC).
  async function wantRefLink(slot) {
    try {
      const res = await api("/reg/ambassador", { method: "POST" });
      renderAmbassadorLink(slot, res);
    } catch (err) {
      if (!isAuthError(err)) say(errorText(err, ""), "warn");
    }
  }

  // Стадия B: ссылка — текстовый узел в моноширинном блоке (НЕ <a href> — делегат должен
  // скопировать, не уйти из приложения кликом, Accessibility 28-UI-SPEC.md), «Скопировать»
  // через Clipboard API + haptic success + чип «Скопировано» (say(), тот же паттерн, что
  // review.js/applications.js), автоскрытие через 2000ms.
  function renderAmbassadorLink(slot, res) {
    const copyBtn = h("button", { class: "btn ghost", type: "button", text: res.copy_button || "" });
    copyBtn.addEventListener("click", async () => {
      if (!res.link || !navigator.clipboard || typeof navigator.clipboard.writeText !== "function") return;
      try {
        await navigator.clipboard.writeText(res.link);
        haptic("success");
        say(res.copied_toast || "", "success");
        setTimeout(() => say(""), 2000);
      } catch (_) {
        // Clipboard API недоступен/отклонён — ссылка всё равно видна текстом, копирование
        // руками остаётся возможным.
      }
    });
    slot.replaceChildren(
      h("div", { class: "ambassador-offer" },
        h("h2", { text: res.heading || "" }),
        h("div", { class: "ambassador-link-box", text: res.link || "" }),
        h("div", { class: "actions" }, copyBtn),
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

  // Квик 260911-w2m: `edit_closed` — вторая, независимая причина того же состояния «сюда
  // сейчас нельзя» (правка выключена, а не регистрация закрыта режимом города) — один блок
  // на оба флага, текст различается, иконка та же (новых ассетов не заводим).
  if (draft.closed || draft.edit_closed) {
    onRefresh = null;
    setMainButton(null);
    holder.replaceChildren(h("section", { class: "state" },
      h("div", { class: "icon" }, icon("clock")),
      h("p", { text: draft.closed_text || draft.edit_closed_text || "" }),
    ));
    return;
  }

  if (draft.handoff) {
    showHandoff(draft.handoff);
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
        if (fresh.handoff) { showHandoff(fresh.handoff); return; }
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
        // UAT 21-12 находка 1 (round 2): касание поля в обзоре меняло только локальный
        // state.setValue(...) (см. open() ниже) — сервер об этом не знал, и на
        // approved-делегате без ранее созданного `reg_drafts` submit не находил, что
        // claim'ить (409 already_submitting, правка молча терялась). PATCH здесь —
        // тот же `collectPatch()`, что уже строит diffView, — единственный источник
        // изменённых полей, отправляется ОДНИМ запросом перед submit.
        const patch = state.collectPatch();
        if (Object.keys(patch).length) {
          d = await api("/reg/draft", { method: "PATCH", body: { version: d.version, answers: patch } });
          // УАТ 10-11.09 (пункт 1): PATCH мог выключить шаг (например, условный курс после
          // смены статуса обучения) — сервер каждый ход пересчитывает enabled_steps заново.
          // `state` держит СПИСОК шагов обзора, не только значения — applyServer() его
          // намеренно не трогает (D-19, чужие правки из чата не должны схлопывать список
          // локально редактируемых полей), поэтому список нужно пересобрать целиком тем же
          // приёмом, что и в мастере (adoptDraft), иначе обзор держит строку уже выключенного
          // сервером шага.
          state = buildFormState(d);
        }
        const res = await api("/reg/draft/submit", { method: "POST" });
        haptic("success");
        renderComplete(res);
      } catch (err) {
        busy = false;
        if (err && err.status === 409 && err.reason === "held_by_bot") {
          // 409 несёт только text — полный набор полей плиты (deeplink/кнопки) берём
          // свежим GET, он уже отдаёт `handoff` целиком.
          try { showHandoff((await api("/reg/draft")).handoff || (err.payload || {})); }
          catch (_) { showHandoff(err.payload || {}); }
          return;
        }
        // UAT 07.09 (находка 1): 400 с полями ошибок — пометить строки + сказать, что именно
        // не так, а не молчать общей веткой ниже (у которой для этого кода нет текста).
        const errors = validationErrors(err);
        if (errors) {
          for (const column of Object.keys(errors)) {
            const el = fieldEls[column];
            // Строка обзора может быть свёрнута — fieldEls[column] тогда пуст, это нормально.
            if (el) setFieldState(el, "error", { text: errors[column] });
          }
          const first = firstFieldError(errors);
          const spec = first && state.specs.find((s) => s.column === first.column);
          const label = spec ? spec.label : "";
          say(label ? `${label}: ${first.text}` : first.text, "warn");
          drawList();
          return;
        }
        if (!isAuthError(err)) {
          const t = errorText(err, "");
          if (t) say(t, "warn");
        }
        drawList();
      }
    }

    // Обзор правки на общих строках с мастером (questionRow, Task 2, обзор 19.1 находка №6):
    // заблокированное поле (город/трек/согласия, D-13) остаётся статичной строкой без
    // pen-line — поведение не меняется, меняется только визуал строки. Незаполненное
    // обязательное получает `q-required`, чтобы отличаться от просто пустой строки.
    function fieldRow(spec) {
      const column = spec.column;
      const locked = Boolean(spec.locked);
      const value = state.value(column);
      // Пункт 4: «заполнен» — по НАБОРУ колонок шага (state.value на каждой), не только по
      // основной — иначе резюме файлом (значение в resume_file_id, не в spec.column) в обзоре
      // считалось незаполненным и получало пометку "q-required".
      const empty = !stepAnswered(spec, state);
      if (locked) {
        return questionRow(h, spec, { value, notSetText: d.not_set_text });
      }
      const panel = h("div", { class: "field hidden" });
      let liveValue = value;
      function open() {
        const wasHidden = panel.classList.contains("hidden");
        panel.classList.add("hidden");
        panel.replaceChildren();
        if (!wasHidden) return;
        // УАТ 10-11.09 (пункт 2): placeholder закрытого списка — реестровый плейсхолдер
        // незаполненного поля (d.not_set_text, reg_form_not_set_text), не литерал JS.
        const el = field(h, { ...spec, placeholder: d.not_set_text }, value, (v) => {
          liveValue = v;
          // Квик 260912-l53: «×» на дропзоне — немедленное удаление, симметрично загрузке
          // ниже (D9). Привязка к spec.type === "file" обязательна: плейсхолдер закрытого
          // списка (selectControl, W1 пункт 2) тоже отдаёт null, и он удалением не является.
          if (v === null && spec.type === "file") {
            removeResume(el, {
              getDraft: () => d, setDraft: (nd) => { d = nd; }, state, column, stepKey: spec.key,
              onDone: () => drawList(),
            });
            return;
          }
          // D9: файл резюме грузится СРАЗУ по выбору, не дожидаясь галки — галка остаётся
          // способом подтвердить текстовый ввод ({text: …}).
          if (typeof File !== "undefined" && v instanceof File) {
            uploadResume(v, el, {
              getDraft: () => d, setDraft: (nd) => { d = nd; }, state, column,
              onDone: () => drawList(),
            });
          }
        });
        fieldEls[column] = el;
        // D13: контакт из Telegram применяется сразу, как файл резюме — то же самое, что
        // делает ручной ввод с последующей галкой, но одним касанием.
        const contactBtn = shareContactButton(h, spec, el, d.share_contact_text, (v) => {
          liveValue = v;
          state.setValue(column, liveValue);
          drawList();
        });
        panel.append(el, ...[contactBtn].filter(Boolean), h("button", {
          class: "btn", type: "button", "aria-label": spec.label,
          onClick: () => {
            state.setValue(column, liveValue);
            drawList();
          },
        }, icon("check")));
        panel.classList.remove("hidden");
      }
      const requiredEmpty = Boolean(spec.required) && empty;
      const row = questionRow(h, spec, {
        value, notSetText: d.not_set_text, onEdit: open,
        extraCls: requiredEmpty ? "q-required" : null,
      });
      return h("div", {}, row, panel);
    }

    function drawList() {
      const list = h("div", { class: "flat-list flush" }, ...state.specs.map(fieldRow));
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
      holder.replaceChildren(...[banner, sectionTitle(h, d.questions_eyebrow), list, diffBox, footer].filter(Boolean));
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
    // Phase 28 (28-05, SU-04, A-03 CONTEXT): «файл» — единственная ветка развилки резюме без
    // своего REG_FLOW-шага на веб-поверхности (бот остаётся в том же состоянии Registration.
    // resume — R2a переиспользует существующий приём документа). Здесь то же самое: шаг
    // "resume" остаётся текущим (stepIndex не двигается), но локально перерисовывается как
    // обычная дропзона (type: "file", тот же контрол, что режим file_or_text). Чисто клиентский
    // флаг — сбрасывается при уходе с шага "resume" в любую сторону.
    let resumeForkBranch = null;

    onRefresh = async () => {
      try {
        const fresh = await api("/reg/draft");
        if (fresh.handoff) { showHandoff(fresh.handoff); return; }
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
            href: fileUrl(item.pdf_file_id),
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

    // Окно списка вопросов вокруг текущего шага (обзор 19.1 находка №5): одна ближайшая
    // отвеченная строка + до двух ближайших предстоящих — текущий шаг в списке не
    // дублируется (он уже на плите и в поле). Остаток сворачивается в `more_questions_text`.
    function drawQuestionWindow(specs) {
      let prevIdx = -1;
      for (let i = stepIndex - 1; i >= 0; i -= 1) {
        // Пункт 4: «отвечен» — по НАБОРУ колонок шага (резюме файлом уходит в
        // resume_file_id/resume_file_name, а не в spec.value основной колонки).
        if (stepAnswered(specs[i], state)) { prevIdx = i; break; }
      }
      const rows = [];
      if (prevIdx >= 0) rows.push(specs[prevIdx]);
      const upcoming = specs.slice(stepIndex + 1, stepIndex + 3);
      rows.push(...upcoming);
      const totalUpcoming = Math.max(0, specs.length - stepIndex - 1);
      const remaining = totalUpcoming - upcoming.length;
      const list = h("div", { class: "flat-list flush" },
        ...rows.map((s) => questionRow(h, s, { notSetText: d.not_set_text })),
      );
      const more = remaining > 0
        ? h("p", { class: "pad faint", text: (d.more_questions_text || "").replace("{n}", String(remaining)) })
        : null;
      if (!rows.length && !more) return null;
      return h("div", {}, sectionTitle(h, d.questions_eyebrow), list, more);
    }

    // Шаги-ветки развилки резюме (28-UI-SPEC.md §1/§3): единственные, где «Назад» ведёт не на
    // предыдущий вопрос анкеты, а на экран выбора способа (A-03 CONTEXT) — тот же закрытый
    // список, что `handlers/reg_extra_steps._FORK_BACK_STEPS` в боте (сверено, не общий
    // импорт — тот же приём дублирования, что литералы «Пропустить»/«Отмена» в проекте).
    const FORK_BACK_STEPS = new Set(["resume_link", "mini_projects", "mini_portfolio", "mini_direction"]);

    // ── шаги анкеты: один вопрос на экран (D-03) ────────────────────────────────────────
    function drawStep() {
      const specs = state.specs;
      if (!specs.length || stepIndex >= specs.length) { submitForm(); return; }
      const rawSpec = specs[stepIndex];
      // Phase 28 (28-05, SU-04): «файл» — клиентская подмена этого же шага дропзоной (см.
      // докстринг `resumeForkBranch` выше) — сервер про эту подмену не знает, stepIndex/шаг
      // черновика не двигаются.
      const spec = (rawSpec.key === "resume" && resumeForkBranch === "file")
        ? { ...rawSpec, type: "file" }
        : rawSpec;
      const column = spec.column;
      const value = state.value(column);

      let liveValue = value;
      // Badge «из прошлой анкеты» (D-07) переиспользует визуал badge/refresh-cw поля
      // (form.js::field() строит один badge-узел на состояние «updated-in-chat» — второго
      // набора DOM-узлов под отдельную иконку «history» общий компонент сегодня не даёт);
      // подпись — параметром из ответа сервера ниже, не литерал. Снимается первым касанием.
      // help: null — подсказку формата уже рисует плита (`plate-sub` ниже); field() рисует
      // spec.help как `.field-help` ВСЕГДА, без этого получились бы два одинаковых абзаца.
      // УАТ 10-11.09 (пункт 2): placeholder закрытого списка — тот же реестровый текст, что
      // и в обзоре правки (d.not_set_text).
      const el = field(h, { ...spec, help: null, placeholder: d.not_set_text }, value, (v) => {
        // Phase 28 (28-05, SU-04, 28-UI-SPEC §1): развилка резюме — тап кнопки И ЕСТЬ переход
        // (никакого «Дальше» на этом экране), поэтому onChange здесь не копит liveValue, а
        // сразу ведёт свою ветку (см. pickResumeBranch ниже).
        if (spec.type === "resume-fork") {
          pickResumeBranch(v);
          return;
        }
        liveValue = v;
        // Квик 260912-l53: «×» на дропзоне — немедленное удаление, симметрично загрузке ниже
        // (D9). Привязка к spec.type === "file" обязательна: плейсхолдер закрытого списка
        // (selectControl, W1 пункт 2) тоже отдаёт null, и он удалением не является.
        if (v === null && spec.type === "file") {
          removeResume(el, { getDraft: () => d, setDraft: (nd) => { d = nd; }, state, column, stepKey: spec.key });
          return;
        }
        // D9: файл резюме грузится СРАЗУ по выбору — goNext() ниже его в JSON PATCH не кладёт
        // (markServerDirty уже отработал здесь).
        if (typeof File !== "undefined" && v instanceof File) {
          uploadResume(v, el, { getDraft: () => d, setDraft: (nd) => { d = nd; }, state, column });
        }
      });
      // D13: контакт из Telegram — та же кнопка, что и в обзоре правки, только без немедленной
      // отправки на сервер (мастер и так шлёт PATCH по кнопке «Дальше»).
      const contactBtn = shareContactButton(h, spec, el, d.share_contact_text, (v) => { liveValue = v; });
      if (spec.value_source === "prior" && !state.isDirty(column)) {
        setFieldState(el, "updated-in-chat", { text: d.prior_badge_text });
        el.addEventListener("input", () => setFieldState(el, "default", {}), { once: true });
        el.addEventListener("change", () => setFieldState(el, "default", {}), { once: true });
      }

      const errorZone = el._nodes && el._nodes.errorZone;

      // Phase 28 (28-05, SU-04): выбор ветки развилки резюме — отдельный PATCH (`resume_type`,
      // закрытый словарь трёх токенов, T-28-05-01), не через общий `answers[column]` (шаг
      // "resume" в режиме fork не пишет свой обычный column вовсе). «file» не двигает шаг
      // (сервер получает `step: null` -> COALESCE сохраняет прежний `reg_drafts.step`,
      // database/db.py::upsert_reg_draft), «link»/«mini» продвигают как обычный шаг — сервер
      // сам находит следующий (уже включённый resume_link/mini_projects, reg_engine.enabled_steps).
      async function pickResumeBranch(code) {
        if (busy) return;
        busy = true;
        setMainButton(null);
        try {
          const res = await api("/reg/draft", {
            method: "PATCH",
            body: { version: d.version, answers: { resume_type: code }, step: code === "file" ? null : spec.key },
          });
          busy = false;
          if (code === "file") {
            // Ветка «файл» — клиентская подмена ЭТОГО ЖЕ шага (resumeForkBranch выше);
            // resume_type уже сохранён сервером (нужен последующим PATCH для enabled_steps),
            // но список шагов/индекс не меняются — adoptDraft() здесь не нужен.
            d = res;
            resumeForkBranch = "file";
            drawStep();
          } else {
            // «link»/«mini» — сервер уже пересчитал enabled_steps (resume_link/mini_projects
            // стали доступны) и вернул res.step = следующий шаг; пересобираем state целиком —
            // applyServer НЕ обновляет state.specs (только значения, form.js::createFormState),
            // тот же приём, что adoptDraft() в drawFork() выше (выбор трека тоже меняет список
            // шагов).
            adoptDraft(res);
            drawCurrent();
          }
        } catch (err) {
          busy = false;
          if (err && err.status === 403 && err.reason === "registration_closed") { showClosed(err); return; }
          if (err && err.status === 409 && err.reason === "held_by_bot") {
            try { showHandoff((await api("/reg/draft")).handoff || (err.payload || {})); }
            catch (_) { showHandoff(err.payload || {}); }
            return;
          }
          if (!isAuthError(err) && errorZone) {
            errorZone.textContent = errorText(err, ""); errorZone.classList.remove("hidden");
          }
          drawStep();
        }
      }

      async function goNext() {
        if (busy) return;
        busy = true;
        setMainButton(null);
        // D9: файл уже уехал на сервер через uploadResume() (markServerDirty) — класть его в
        // JSON PATCH нельзя (`JSON.stringify(File)` даёт "{}", сервер отвечает 400). Указатель
        // шага всё равно сдвигаем — answers пустой, step идёт отдельным полем.
        const isFileValue = typeof File !== "undefined" && liveValue instanceof File;
        const patch = {};
        if (!isFileValue) {
          state.setValue(column, liveValue);
          patch[column] = liveValue;
        }
        try {
          const res = await api("/reg/draft", {
            method: "PATCH", body: { version: d.version, answers: patch, step: spec.key },
          });
          // УАТ 10-11.09 (пункт 1): сервер на КАЖДОМ PATCH пересчитывает enabled_steps — шаг,
          // выключенный только что данным ответом (условный курс/специальность и т.п.),
          // обязан исчезнуть из мастера сразу, а не после перезахода. `applyServer` намеренно
          // обновляет только значения (D-19, чужие правки из чата не двигают список шагов),
          // поэтому здесь нужна ПОЛНАЯ пересборка — тот же приём, что уже применяет
          // `pickResumeBranch`/`drawFork` при смене трека (`adoptDraft`). `res.step` — это
          // «ещё не отвеченный» шаг (form.py переводит «только что отвеченный» в «следующий»
          // один раз, на сервере); самовыключающихся шагов в `enabled_steps` нет — условие
          // цикла «сервер вернул тот же шаг» не возникает. `STEP_DONE` сам уводит мастер на
          // отправку (`stepIndexFromKey` → `specs.length`).
          adoptDraft(res);
          busy = false;
          drawStep();
        } catch (err) {
          busy = false;
          if (err && err.status === 400 && err.reason === "invalid" && err.payload && err.payload.errors) {
            const msg = err.payload.errors[column];
            if (errorZone && msg) { errorZone.textContent = msg; errorZone.classList.remove("hidden"); }
            setMainButton(null, null);
            setMainButton(d.next_cta_text || null, goNext);
          } else if (err && err.status === 403 && err.reason === "registration_closed") {
            showClosed(err);
          } else if (err && err.status === 409 && err.reason === "held_by_bot") {
            try { showHandoff((await api("/reg/draft")).handoff || (err.payload || {})); }
            catch (_) { showHandoff(err.payload || {}); }
          } else if (!isAuthError(err)) {
            if (errorZone) { errorZone.textContent = errorText(err, ""); errorZone.classList.remove("hidden"); }
            setMainButton(d.next_cta_text || null, goNext);
          }
        }
      }

      // Phase 28 (28-05, SU-04): «Пропустить» второго мини-подшага (mini_portfolio) — та же
      // семантика, что в боте (validate_answer пишет «-»), третья футер-кнопка ниже.
      async function goSkip() {
        if (busy) return;
        liveValue = "-";
        await goNext();
      }

      function goBack() {
        if (busy) return;
        // Phase 28 (28-05, SU-04, A-03 CONTEXT): единственные исключения из «Назад = предыдущий
        // вопрос» — ветка «файл» (клиентская подмена этого же шага) и четыре шага-ветки
        // (resume_link/mini_*) — все возвращают на экран развилки (R1), не на stepIndex-1.
        if (rawSpec.key === "resume" && resumeForkBranch === "file") {
          resumeForkBranch = null;
          drawStep();
          return;
        }
        if (FORK_BACK_STEPS.has(rawSpec.key)) {
          resumeForkBranch = null;
          stepIndex = stepIndexFromKey(state.specs, "resume");
          drawStep();
          return;
        }
        stepIndex = Math.max(0, stepIndex - 1);
        drawStep();
      }

      const showProgress = Boolean(d.show_progress) && specs.length > 0;
      const progressLabel = (d.progress_text || "")
        .replace("{step}", String(stepIndex + 1)).replace("{total}", String(specs.length));

      // Полоса прогресса мастера: во всю ширину, событийным цветом. Заливку двигает CSSOM,
      // не атрибут style — политика CSP приложения (`style-src 'self'` без `'unsafe-inline'`,
      // miniapp/main.py) молча отбрасывает style="…", запись в el.style ею не ограничена.
      let progressRow = null;
      if (showProgress) {
        const fill = h("div", { class: "wizard-progress-fill" });
        fill.style.width = `${Math.round(((stepIndex + 1) / specs.length) * 100)}%`;
        const bar = h("div", {
          class: "wizard-progress flush", role: "progressbar",
          "aria-valuenow": String(stepIndex + 1), "aria-valuemin": "1", "aria-valuemax": String(specs.length),
          "aria-label": progressLabel,
        }, fill);
        progressRow = h("div", { class: "wizard-progress-row" },
          bar,
          d.exists ? h("span", { class: "wizard-progress-note", text: d.draft_saved_text || "" }) : null,
        );
      }

      // Плита шага: номер + вопрос крупным курсивом (обзор 19.1 находка №5) — тумблер
      // reg_show_progress выключен целиком гасит номерной блок, а не показывает его пусто.
      const plateRow = showProgress
        ? h("div", { class: "plate-row" },
          h("span", { class: "plate-big", text: String(stepIndex + 1).padStart(2, "0") }),
          h("span", { class: "plate-total", text: `/ ${specs.length}` }),
          h("span", { class: "plate-eyebrow", text: progressLabel }),
        )
        : null;
      const plate = h("section", { class: "plate plate--form" },
        plateRow,
        h("h1", { text: spec.prompt || spec.label }),
        spec.help ? h("p", { class: "plate-sub", text: spec.help }) : null,
      );

      // Phase 28 (28-05, SU-04, 28-UI-SPEC §1/§3): развилка резюме — тап кнопки И ЕСТЬ переход,
      // футера «Дальше» на этом экране нет вовсе (isForkPick); mini_portfolio — единственный
      // шаг с третьей футер-кнопкой «Пропустить» (spec.skip_label публикует ТОЛЬКО этот шаг,
      // reg_engine.step_spec — остальные skip_allowed шаги не меняются ни на байт, D-06).
      const isForkPick = spec.type === "resume-fork";
      const footer = h("div", { class: "task-actions" },
        stepIndex > 0
          ? h("button", { class: "btn ghost", type: "button", "aria-label": d.back_cta_text || "", onClick: goBack },
            icon("arrow-right", { class: "icon-flip" }), h("span", { text: d.back_cta_text || "" }))
          : null,
        spec.skip_label
          ? h("button", { class: "btn ghost", type: "button", "aria-label": spec.skip_label, onClick: goSkip },
            h("span", { text: spec.skip_label }))
          : null,
        isForkPick
          ? null
          : h("button", { class: "btn", type: "button", disabled: busy, "aria-label": d.next_cta_text || "", onClick: goNext },
            h("span", { text: d.next_cta_text || "" }), icon("arrow-right")),
      );

      holder.replaceChildren(...[
        progressRow,
        plate,
        h("div", { class: "wizard-field" }, el, contactBtn),
        drawQuestionWindow(specs),
        chatLink(d.continue_in_chat_text, d.continue_deeplink),
        footer,
      ].filter(Boolean));
      setMainButton(isForkPick ? null : (d.next_cta_text || null), goNext, { disabled: busy });
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
