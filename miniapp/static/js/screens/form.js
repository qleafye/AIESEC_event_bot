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
import { fileUrl, flatRow, sectionTitle, labelText, noticeBox, screenText, formV2Text } from "../ui.js";
import { icon } from "../icons.js";
import { haptic, slideIn, progressTo } from "../motion.js";
import { applyTheme, themeOverride, setThemeOverride } from "../app.js";

// Phase 30 (30-05, задача 4, A2-08, T-30-13): личный override вибрации анкеты — ПОВЕРХ
// тумблера `reg_form_haptics`, но менеджерский тумблер СИЛЬНЕЕ (порядок проверки в
// `wizardHaptic` ниже: сначала flags.haptics, потом этот флаг). Клиентский, в БД не пишется —
// тот же localStorage-приём, что онбординг-флаг 19.1/`themeOverride` выше.
const HAPTICS_OVERRIDE_KEY = "aiesec_miniapp_form_haptics_override_v1"; // "on" | "off"

function hapticsOverrideOn() {
  try {
    return localStorage.getItem(HAPTICS_OVERRIDE_KEY) !== "off";
  } catch (_) {
    return true;
  }
}

function setHapticsOverride(on) {
  try {
    localStorage.setItem(HAPTICS_OVERRIDE_KEY, on ? "on" : "off");
  } catch (_) { /* приватный режим/недоступный localStorage — override просто не сохранится */ }
}

// Единый хелпер вибрации всего модуля анкеты (задача 4, "Вызовы tg.HapticFeedback... идут
// через один хелпер") — используют попап настроек и попытка тапа по его сегментам; остальные
// `haptic("success")` файла (копирование ссылки амбассадора, финальная отправка) — старое
// поведение обеих версий анкеты, эта фаза его не трогает (не относится к новым 30-05
// настройкам темы/языка/вибрации).
function wizardHaptic(flags) {
  if (!flags || !flags.haptics || !hapticsOverrideOn()) return;
  haptic("light");
}

const AUTH_EXCEPT_REASONS = [];
function isAuthError(err) {
  return isAuthErrorBase(err, AUTH_EXCEPT_REASONS);
}

// Квик 12.09 (UI-аудит, пункт 3): 12 мест ниже звали errorText(err, "") — обрыв сети даёт
// ответ БЕЗ payload.text, и делегат видел пустую плашку вместо человеческого текста. Общий
// фоллбэк из оболочки (data-screen-texts, page.py::SCREEN_TEXT_KEYS) вместо пустой строки —
// одно место правки на все 12 (заменены на failText(err) ниже).
function failText(err) {
  return errorText(err, screenText("network_error"));
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

// Phase 30 (30-05, задача 2, A2-07, 30-UI-SPEC.md § «Обзор перед отправкой»): маппинг шага на
// одну из трёх групп обзора — ОДНА таблица, а не условия по месту (план прямо требует). Шаг вне
// таблицы (будущий REG_FLOW-шаг, который планировщик забыл вписать) попадает в третью группу
// «Форум» — она и так собирает организационные/событийные вопросы, безопасный дефолт.
const REVIEW_GROUPS = {
  // Живой прогон 16.09 (п.3в): ФИО отсутствовало в таблице вовсе и падало в дефолтную
  // «Форум» — первый шаг анкеты (приёмка 16.09, п.1) обязан лежать в «О тебе», как остальные
  // личные данные.
  full_name: "about",
  age: "about", phone: "about", alumni_status: "about", vk: "about", city: "about",
  email: "about", local_committee: "about", position: "about", department: "about",
  aiesec_role: "about", needs_certificate: "about", allergies: "about", food_pref: "about",
  birth_date: "about",
  education_status: "study", course: "study", university: "study", study_field: "study",
  stack: "study", experience: "study", readiness: "study", resume: "study",
  resume_link: "study", mini_projects: "study", mini_portfolio: "study",
  mini_direction: "study", case_optin: "study", specialty: "study", work_status: "study",
  work_sphere: "study", missing_skills: "study", english_level: "study",
};

function reviewGroupOf(stepKey) {
  return REVIEW_GROUPS[stepKey] || "event";
}

// Composite-шаг («Образование») — единственное исключение из однострочного паттерна обзора
// (30-UI-SPEC.md): двустрочная строка, вторая строка — сводка значений включённых частей через
// «·». Значения частей публикует `reg_engine.form_spec` (план 30-05, задача 0б) — второй раз их
// не считаем.
function compositeSummary(spec) {
  const parts = (spec.composite && spec.composite.parts) || [];
  return parts.map((p) => p.value).filter((v) => v != null && v !== "").join(" · ");
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
// Phase 30 (30-03, A2-08, 30-UI-SPEC.md § «7. text»): `opts.iconName`/`opts.hint` —
// рестайл ТОЛЬКО у мастера новой анкеты (вызывающий передаёт их при `degraded_kind !==
// "legacy"`, см. `drawStep()` ниже); обзор точечной правки (второй call site, line ~523)
// их не передаёт — там кнопка остаётся byte-в-byte прежней (иконка `smartphone`, без
// подсказки), 21-UI-SPEC этот экран не переопределяет. Сам вызов `requestContact` —
// БЕЗ изменений (30-PATTERNS.md: «не реализуем второй раз»).
function shareContactButton(h, spec, el, text, onNumber, opts = {}) {
  if (spec.type !== "phone" || !canShareContact(tgRef)) return null;
  const button = h("button", {
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
  }, icon(opts.iconName || "smartphone"), h("span", { text: text || "" }));
  if (!opts.hint) return button;
  return h("div", {}, button, h("p", { class: "label-role", text: opts.hint }));
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

  const { el: notice, say } = noticeBox(h);
  const holder = h("div");
  root.append(notice, holder);

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
    // Квик 260915-twr (Task A): `showHandoff` живёт вне замыканий мастера/обзора — переменной
    // `busy` не видит, поэтому свой локальный гвард на обе кнопки плиты. Без него пять тапов
    // подряд по «Забрать сюда» ставили пять `reg_fsm_reset` (см. draft_takeover), а двойной тап
    // по «Продолжить в чате» слал два `release`.
    let handoffBusy = false;
    const continueBtn = h("button", { class: "btn ghost", type: "button", onClick: async () => {
      if (handoffBusy) return;
      handoffBusy = true;
      continueBtn.disabled = true;
      takeoverBtn.disabled = true;
      try { await api("/reg/draft/release", { method: "POST" }); } catch (_) { /* fail-soft: чат всё равно откроется */ }
      continueInChat(handoff.deeplink);
    } }, icon("message-circle"), h("span", { text: handoff.continue_text || "" }));
    const takeoverBtn = h("button", { class: "btn", type: "button", onClick: async () => {
      if (handoffBusy) return;
      handoffBusy = true;
      continueBtn.disabled = true;
      takeoverBtn.disabled = true;
      try {
        const res = await api("/reg/draft/takeover", { method: "POST" });
        if (res.kind === "edit") await renderOverview(res);
        else await renderWizard(res);
      } catch (err) {
        handoffBusy = false;
        continueBtn.disabled = false;
        takeoverBtn.disabled = false;
        if (!isAuthError(err)) say(failText(err), "warn");
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
          // Квик 12.09 (UI-аудит, пункт 2): раньше — одна иконка check без текста и без
          // aria-label (accessibility BLOCKER на самом просматриваемом терминальном экране).
          h("button", { class: "btn", type: "button", "aria-label": res.home_cta || "", onClick: goHome },
            h("span", { text: res.home_cta || "" }), icon("arrow-right")),
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
      if (!isAuthError(err)) say(failText(err), "warn");
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
        // Приёмка 17.09 (п.1): пояснение — где эту ссылку найти потом (голый URL без контекста
        // выше не объясняет ничего сам по себе). `.ambassador-offer p` уже стилизован (muted),
        // второй класс не заводим.
        res.note ? h("p", { text: res.note }) : null,
        h("div", { class: "actions" }, copyBtn),
      ),
    );
  }

  let draft;
  try {
    draft = await api("/reg/draft");
  } catch (err) {
    if (!isAuthError(err)) holder.replaceChildren(h("p", { class: "error-inline", text: failText(err) }));
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
          if (!isAuthError(err)) say(failText(err), "warn");
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
          const t = failText(err);
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
    // Quick 260915-4mw (ANIM-01/02): направление въезда контента шага ("fwd" по умолчанию,
    // goBack() и возврат на развилку резюме ставят "back") + отметка прогресса прошлого
    // рендера (drawStep() двигает полосу ОТ неё, не с нуля — "доезжает", а не перепрыгивает).
    let stepDir = "fwd";
    let lastProgress = null;

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
            showError(err.payload.errors[item.field] || failText(err));
          } else if (err && err.status === 409 && err.reason === "already_set") {
            // Значение уже зафиксировано в чате — перечитать черновик, экран исчезнет сам.
            try { adoptDraft(await api("/reg/draft")); } catch (_) { /* фон — экран не падает */ }
            drawCurrent();
          } else if (err && err.status === 403 && err.reason === "registration_closed") {
            showClosed(err);
          } else if (!isAuthError(err)) {
            showError(failText(err));
          }
        }
      }

      holder.replaceChildren(
        h("div", { class: "wizard-step" },
          el,
          h("div", { class: "task-actions" },
            // Живой прогон 16.09 (п.1): кнопка подтверждения (город/формат участия) была
            // только иконкой check — делегат не понимал, что это кнопка подтверждения выбора.
            // Подпись — тот же `d.next_cta_text` («Дальше»), что и на остальных шагах мастера
            // (один и тот же глагол действия на всей анкете, без нового реестрового ключа).
            h("button", {
              class: "btn", type: "button", disabled: busy, "aria-label": item.text || "", onClick: next,
            }, icon("check"), h("span", { text: d.next_cta_text || "" })),
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
          if (cb.checked) {
            signedConsents.add(item.key);
            // Приёмка 16.09 («автопереход для всех вопросов, которые могут автоскипаться»):
            // галка на ПОСЛЕДНЕМ обязательном согласии — законченный ответ этого экрана,
            // дальше нечего отмечать. Тот же переход `next()`, что кнопка-галочка ниже (сама
            // `next()` перечитывает `signedConsents` заново — второй проверки полноты не
            // заводим). Снятие галки НЕ коммитит — делегат мог передумать.
            if (consentItems.every((it) => signedConsents.has(it.key))) {
              setTimeout(() => next(), 0);
            }
          } else {
            signedConsents.delete(item.key);
          }
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
            errorBox.textContent = failText(err);
            errorBox.classList.remove("hidden");
          }
        }
      }

      holder.replaceChildren(
        h("div", { class: "wizard-step" },
          ...cards, errorBox,
          h("div", { class: "task-actions" },
            // Живой прогон 16.09 (п.1): та же правка, что у экрана города/формата выше —
            // подпись рядом с иконкой, без неё кнопка выглядела нерабочей.
            h("button", {
              class: "btn", type: "button", disabled: busy, "aria-label": d.next_cta_text || "", onClick: next,
            }, icon("check"), h("span", { text: d.next_cta_text || "" })),
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

    // Phase 30 (30-05, задача 4, A2-08, 30-UI-SPEC.md § «Настройки в шапке анкеты»): поповер
    // темы/языка/вибрации — виден только при `flags.header_settings`. Строится заново на
    // каждый вызов (та же дисциплина, что `holder.replaceChildren` у шага/обзора — состояние
    // выбора темы/вибрации живёт в localStorage, не в этом замыкании), закрывается тапом
    // мимо листа или после смены языка (шаг перерисовывается заново).
    function buildHeaderSettingsGear(flags) {
      if (!flags || !flags.header_settings) return null;
      const backdrop = h("div", { class: "sheet-backdrop hidden" });
      const sheet = h("div", { class: "sheet" });
      backdrop.append(sheet);
      function close() { backdrop.classList.add("hidden"); }
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });

      function segRow(labelText_, options, currentValue, onPick) {
        const seg = h("div", { class: "hub-seg" });
        for (const [value, text] of options) {
          const btn = h("button", {
            class: `hub-seg-btn${value === currentValue ? " active" : ""}`, type: "button", text: text || "",
            onClick: () => {
              wizardHaptic(flags);
              for (const b of seg.children) b.classList.remove("active");
              btn.classList.add("active");
              onPick(value);
            },
          });
          seg.append(btn);
        }
        return h("div", {}, labelText_ ? h("p", { class: "label-role", text: labelText_ }) : null, seg);
      }

      sheet.append(segRow(
        formV2Text("header_settings_theme_label"),
        [
          ["auto", formV2Text("header_settings_theme_auto")],
          ["dark", formV2Text("header_settings_theme_dark")],
          ["light", formV2Text("header_settings_theme_light")],
        ],
        themeOverride(),
        (value) => setThemeOverride(value),
      ));

      // Язык анкеты — только когда включён модуль (фаза 27); переключение зовёт существующий
      // механизм (POST /reg/lang -> set_user_lang) и перерисовывает ТЕКУЩИЙ шаг заново, ответы
      // не трогает (30-CONTEXT.md реш. 7).
      if (d.lang_module_enabled) {
        sheet.append(segRow(
          formV2Text("header_settings_lang_label"), [["ru", "RU"], ["en", "EN"]], d.lang,
          async (value) => {
            if (value === d.lang) return;
            try {
              await api("/reg/lang", { method: "POST", body: { lang: value } });
              const fresh = await api("/reg/draft");
              d = fresh;
              state = buildFormState(fresh);
              stepIndex = stepIndexFromKey(state.specs, fresh.step);
              close();
              drawCurrent();
            } catch (_) {
              // fail-soft: язык не переключился — поповер остаётся открытым, ответы целы.
            }
          },
        ));
      }

      sheet.append(segRow(
        formV2Text("header_settings_haptics_label"),
        [["on", formV2Text("header_settings_haptics_on")], ["off", formV2Text("header_settings_haptics_off")]],
        hapticsOverrideOn() ? "on" : "off",
        (value) => setHapticsOverride(value === "on"),
      ));

      sheet.append(h("p", { class: "label-role", text: formV2Text("header_settings_scope_note") || "" }));

      const gear = h("button", {
        class: "wizard-header-gear", type: "button",
        "aria-label": formV2Text("header_settings_theme_label") || "",
        onClick: () => { wizardHaptic(flags); backdrop.classList.remove("hidden"); },
      }, icon("settings"));

      return { gear, backdrop };
    }

    // ── шаги анкеты: один вопрос на экран (D-03) ────────────────────────────────────────
    function drawStep() {
      const specs = state.specs;
      if (!specs.length || stepIndex >= specs.length) {
        // Phase 30 (30-05, задача 2, A2-07): все ответы даны — новая анкета (master switch
        // `reg_form_v2_enabled`, публикуется В КАЖДОМ `spec.flags` одинаково — `form_spec()`
        // считает флаги один раз на форму, 30-03) сначала показывает обзор, легаси-анкета
        // отправляется как раньше, без единого изменения байта.
        const isV2Form = specs.length > 0 && specs[0].flags && specs[0].flags.v2_enabled;
        if (isV2Form) { drawReview(); return; }
        submitForm();
        return;
      }
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
      const el = field(h, { ...spec, help: null, placeholder: d.not_set_text }, value, (v, opts) => {
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
          uploadResume(v, el, {
            getDraft: () => d, setDraft: (nd) => { d = nd; }, state, column,
            // Приёмка 16.09: commit — только ПОСЛЕ того, как сервер принял файл (`onDone`
            // здесь зовётся из uploadResume уже после успешного POST /uploads), не на самом
            // выборе файла — тот же отложенный тик, что `opts.commit` ниже, второй проверки
            // валидности не заводим (currentMainDisabled — общая точка).
            onDone: () => { setTimeout(() => { if (!currentMainDisabled()) goNext(); }, 0); },
          });
          return;
        }
        // Приёмка 16.09 (п.4 «при автозаполнении сразу переходить на следующий вопрос»):
        // контрол пометил ответ законченным (тап по плитке `select`, по чипу/подсказке
        // справочника) — переходим тем же `goNext`, что и кнопка «Далее», а не второй веткой
        // отправки. Мультивыбор, текстовые поля и карточка-композит `commit` не ставят вовсе
        // (form_types.js), так что отдельного списка исключений здесь нет.
        //
        // Переход отложен на следующий тик: `notify()` контрола (а с ним и `footerDisabled`,
        // на который смотрит `currentMainDisabled`) отрабатывает ПОСЛЕ onChange — проверь мы
        // валидность прямо здесь, читали бы состояние прошлого нажатия.
        if (opts && opts.commit) {
          setTimeout(() => { if (!currentMainDisabled()) goNext(); }, 0);
        }
      });
      // D13: контакт из Telegram — та же кнопка, что и в обзоре правки, только без немедленной
      // отправки на сервер (мастер и так шлёт PATCH по кнопке «Дальше»).
      // Phase 30 (30-03, A2-08, 30-UI-SPEC.md § «7. text»): v2-рестайл ТОЛЬКО у мастера новой
      // анкеты — иконка `phone-outgoing` + подсказка под кнопкой из `spec.v2_texts`; обзор
      // точечной правки (второй call site `shareContactButton`) не трогается.
      // Phase 30 (30-08, задача 2, найдено съёмкой скриншотов): `isV2` читалась здесь ДО
      // собственного объявления (`const isV2 = …` ниже по функции, TDZ) — `ReferenceError:
      // Cannot access 'isV2' before initialization` на КАЖДОМ шаге мастера, как только
      // включался `reg_form_v2_enabled` (существующие тесты/скриншоты до этой задачи не
      // прогоняли живой рендер `drawStep()` с v2 включённым — только `buildV2Control()`
      // изолированно). Объявление поднято сюда (единственное место объявления, дальше по
      // функции — не дублируется).
      const isV2 = !!(spec.degraded_kind && spec.degraded_kind !== "legacy");
      const v2Texts = spec.v2_texts || {};
      const contactBtn = shareContactButton(
        h, spec, el,
        (isV2 && v2Texts.phone_share_button) || d.share_contact_text,
        (v) => {
          liveValue = v;
          // Приёмка 16.09 (п. «номер телефона не работает автопереход»): «Поделиться номером»
          // — один тап даёт законченный ответ, тот же отложенный `goNext`, что `opts.commit`
          // ниже (этот колбэк — не `field()`.onChange, второй копии проверки не заводим,
          // просто вызываем тот же переход). Ручной ввод номера (textControl) commit не
          // ставит — «Далее» там остаётся за делегатом, как раньше.
          setTimeout(() => { if (!currentMainDisabled()) goNext(); }, 0);
        },
        isV2 ? { iconName: "phone-outgoing", hint: v2Texts.phone_share_hint } : {},
      );
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
            errorZone.textContent = failText(err); errorZone.classList.remove("hidden");
          }
          drawStep();
        }
      }

      // Первая ошибка чужой колонки — с подписью части карточки («Программа: …»), если сервер
      // прислал её в спеке композита; подписи берутся из данных ответа, литералов здесь нет.
      function compositeErrorText(errors) {
        const parts = (spec.composite && spec.composite.parts) || [];
        for (const [errColumn, text] of Object.entries(errors || {})) {
          const part = parts.find((p) => p.column === errColumn || p.key === errColumn);
          const partLabel = part ? labelText(part.label) : "";
          return partLabel ? `${partLabel}: ${text}` : text;
        }
        return "";
      }

      async function goNext() {
        if (busy) return;
        busy = true;
        setMainButton(null);
        // D9: файл уже уехал на сервер через uploadResume() (markServerDirty) — класть его в
        // JSON PATCH нельзя (`JSON.stringify(File)` даёт "{}", сервер отвечает 400). Указатель
        // шага всё равно сдвигаем — answers пустой, step идёт отдельным полем.
        const isFileValue = typeof File !== "undefined" && liveValue instanceof File;
        // Phase 30 (30-05, задача 0б, хвост 30-04): composite-карточка «Образование»
        // (`form_types.js::compositeCard`) отдаёт onChange ОБЪЕКТОМ `{step_key: value, ...}` —
        // патч НЕСКОЛЬКИХ колонок одной карточкой, а не скаляром одной колонки `column`, как
        // остальные типы. Отличаем от файла (`instanceof File`) и repeatable-массива
        // (`Array.isArray`) — только «голый» объект значит composite-патч; для образования
        // `step_key === column` (`STEP_TO_COLUMN` — identity-мэп для этих полей, reg_engine.py),
        // второй карты имён не заводим.
        // Приёмка 15.09 (п.5 «при отправке текста в резюме пишется, что не дошло до сервера»):
        // объектом отдаёт onChange не только карточка-композит — дропзона резюме шлёт текстовый
        // ответ как `{text: "..."}` (`form.js::fileControl`, распаковывает его сервер,
        // `routers/form.py::_unwrap_other`). По одной лишь ФОРМЕ значения их не различить, и
        // текст резюме уезжал колонкой «text» -> `400 bad_field` -> общий текст «не дошло до
        // сервера». Признак composite-патча — СПЕКА шага (`spec.composite` строит только
        // `form_spec` для карточки), а не форма значения.
        const isCompositePatch = !isFileValue && liveValue !== null
          && typeof liveValue === "object" && !Array.isArray(liveValue)
          && Boolean(spec.composite);
        const patch = {};
        if (isCompositePatch) {
          for (const [patchColumn, patchValue] of Object.entries(liveValue)) {
            state.setValue(patchColumn, patchValue);
            patch[patchColumn] = patchValue;
          }
        } else if (!isFileValue) {
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
            // Приёмка 15.09 (п.2/п.3а «кнопка далее не работает», «сработало со второго раза»):
            // карточка-композит патчит НЕСКОЛЬКО колонок, и ошибка приходила по колонке части
            // (`university`), а не по колонке шага (`education_status`) — сообщение молча
            // терялось, «Далее» выглядела мёртвой. Берём ошибку своей колонки, если она есть,
            // иначе первую пришедшую — с названием части, чтобы делегат знал, что чинить.
            const msg = err.payload.errors[column] || compositeErrorText(err.payload.errors);
            if (errorZone && msg) { errorZone.textContent = msg; errorZone.classList.remove("hidden"); }
            setMainButton(null, null);
            // Phase 30 (30-03): подпись после ошибки — та же, что была на кнопке до запроса
            // (v2-типы держат свою через `currentMainLabel()`, легаси — `d.next_cta_text`
            // без изменений).
            setMainButton(currentMainLabel(), goNext);
          } else if (err && err.status === 403 && err.reason === "registration_closed") {
            showClosed(err);
          } else if (err && err.status === 409 && err.reason === "held_by_bot") {
            try { showHandoff((await api("/reg/draft")).handoff || (err.payload || {})); }
            catch (_) { showHandoff(err.payload || {}); }
          } else if (!isAuthError(err)) {
            if (errorZone) { errorZone.textContent = failText(err); errorZone.classList.remove("hidden"); }
            setMainButton(currentMainLabel(), goNext);
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
        stepDir = "back"; // quick 260915-4mw: все три ветки ниже зовут drawStep()
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
        // Quick 260915-4mw (ANIM-02): доезжает трансформом от прошлой отметки, не перепрыгивает
        // (motion.js::progressTo); первая отрисовка шага — from === to, анимации не видно.
        const ratio = (stepIndex + 1) / specs.length;
        progressTo(fill, lastProgress === null ? ratio : lastProgress, ratio);
        lastProgress = ratio;
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
      // Phase 30 (30-03, A2-08): плита сама уже существует байт-в-байт для ЛЮБОГО шага с
      // Phase 23.1 (`.plate--form`) — 30-UI-SPEC.md описывает её как «новую» ошибочно
      // (сверено с кодом задачей 4). Единственная фактическая правка — роль Step Title
      // (курсив 27px вместо некурсивного 28px) на самом заголовке, и ТОЛЬКО когда
      // `degraded_kind != "legacy"` — класс `.step-title` ниже, легаси-путь не трогается.
      const plateRow = showProgress
        ? h("div", { class: "plate-row" },
          h("span", { class: "plate-big", text: String(stepIndex + 1).padStart(2, "0") }),
          h("span", { class: "plate-total", text: `/ ${specs.length}` }),
          h("span", { class: "plate-eyebrow", text: progressLabel }),
        )
        : null;
      const plate = h("section", { class: "plate plate--form" },
        plateRow,
        h("h1", { text: spec.prompt || spec.label, class: isV2 ? "step-title" : null }),
        spec.help ? h("p", { class: "plate-sub", text: spec.help }) : null,
      );

      // Phase 28 (28-05, SU-04, 28-UI-SPEC §1/§3): развилка резюме — тап кнопки И ЕСТЬ переход,
      // футера «Дальше» на этом экране нет вовсе (isForkPick); mini_portfolio — единственный
      // легаси-шаг с третьей футер-кнопкой «Пропустить» (spec.skip_label публикует ТОЛЬКО этот
      // шаг, reg_engine.step_spec — остальные skip_allowed шаги не меняются ни на байт, D-06).
      //
      // Phase 30 (30-03, A2-02/A2-08, 30-UI-SPEC.md §§ «5. multi»/«1. select»): у v2-типов —
      // ОДНА кнопка вместо пары «Пропустить»/«Дальше», подпись и disabled — из `footerLabel`/
      // `footerDisabled`, которые `field()` положила в `el._nodes` (buildV2Control, план
      // 30-03 задачи 2/3). `onFooterChange` обновляет ОБЕ поверхности (`.btn` футера и
      // нативный `MainButton`) без полной пересборки шага — drawStep() вызывается один раз
      // за шаг (см. докстринг файла), сам onChange контрола этого не делает.
      const isForkPick = spec.type === "resume-fork";
      const footerNodes = el._nodes || {};
      let v2Label = footerNodes.footerLabel || null;
      let v2Disabled = !!footerNodes.footerDisabled;

      function currentMainLabel() {
        return (isV2 && v2Label) ? v2Label : (d.next_cta_text || "");
      }
      function currentMainDisabled() {
        return busy || (isV2 && v2Disabled);
      }
      function syncMainButton() {
        if (mainLabelNode) mainLabelNode.textContent = currentMainLabel();
        if (mainBtnEl) {
          mainBtnEl.disabled = currentMainDisabled();
          mainBtnEl.setAttribute("aria-label", currentMainLabel());
        }
        setMainButton(isForkPick ? null : currentMainLabel(), goNext, { disabled: currentMainDisabled() });
      }

      const mainLabelNode = h("span", { text: currentMainLabel() });
      const mainBtnEl = isForkPick ? null : h("button", {
        class: "btn", type: "button", disabled: currentMainDisabled(), "aria-label": currentMainLabel(),
        onClick: goNext,
      }, mainLabelNode, icon("arrow-right"));

      if (isV2 && footerNodes.onFooterChange) {
        footerNodes.onFooterChange((label, disabled) => {
          v2Label = label;
          v2Disabled = !!disabled;
          syncMainButton();
        });
      }

      const footer = h("div", { class: "task-actions" },
        stepIndex > 0
          ? h("button", { class: "btn ghost", type: "button", "aria-label": d.back_cta_text || "", onClick: goBack },
            icon("arrow-right", { class: "icon-flip" }), h("span", { text: d.back_cta_text || "" }))
          : null,
        mainBtnEl,
      );

      // Приёмка 15.09 (п.4 «нет кнопки скип в UI анкете»): необязательный шаг обязан иметь
      // видимый способ пропуска на ЛЮБОЙ анкете — в чате кнопка «Пропустить» есть всегда, а
      // новая анкета её теряла: пустой ответ сервер не принимает (`validate_answer` ждёт
      // хотя бы «-»), и делегат упирался в тупик. Исключение одно — `multi`: там пропуск уже
      // живёт подписью САМОЙ главной кнопки (30-UI-SPEC.md § «5. multi»: «одна кнопка»),
      // вторая кнопка рядом была бы дублем.
      //
      // Приёмка 16.09 (п.2 «кнопку "Пропустить" ставим под местом, где вписываем ответ —
      // внизу не видно»): кнопка больше не в футере экрана (он уезжает под окно «что дальше»
      // и ссылку на чат — на телефоне это ниже сгиба), а прямо под контролом ответа, перед
      // зоной ошибки. Узел `errorZone` — тот же якорь, что уже читает `drawFork` ниже;
      // обработчик и подпись прежние.
      if (spec.skip_label && !(isV2 && spec.degraded_kind === "multi")) {
        const skipBtn = h("button", {
          class: "btn ghost field-skip", type: "button",
          "aria-label": spec.skip_label, onClick: goSkip,
        }, h("span", { text: spec.skip_label }));
        if (footerNodes.errorZone && el.insertBefore) el.insertBefore(skipBtn, footerNodes.errorZone);
        else el.append(skipBtn);
      }

      // Phase 30 (30-05, задача 4): шестерёнка — над плитой, видна только при
      // `flags.header_settings` (степень v2, но НЕ зависит от `isV2`/`degraded_kind` шага —
      // тумблер отдельный, может быть включён у легаси-типа тоже).
      const headerSettings = buildHeaderSettingsGear(spec.flags);
      const headerRow = headerSettings ? h("div", { class: "wizard-header-row" }, headerSettings.gear) : null;

      // Quick 260915-4mw (ANIM-02): узлы шага именованы отдельно от replaceChildren — после
      // отрисовки едет въездом только контент шага, шапка/полоса прогресса остаются на месте
      // (заголовок и прогресс — не «шаг», а рамка вокруг него). Состав и порядок узлов те же,
      // что были в inline-массиве раньше — новых обёрток в DOM не добавлено.
      const stepNodes = [
        headerRow,
        progressRow,
        plate,
        // Квик 12.09 (UI-аудит, пункт 9): пояснение шага (например case_optin.description) —
        // Body-роль (28-UI-SPEC §5, 15px, var(--text)), поэтому живёт ВНЕ акцентной плиты, а
        // не среди spec.prompt/spec.help, которые плита уже рисует выше.
        spec.description ? h("p", { class: "step-description", text: spec.description }) : null,
        h("div", { class: "wizard-field" }, el, contactBtn),
        drawQuestionWindow(specs),
        chatLink(d.continue_in_chat_text, d.continue_deeplink),
        footer,
        headerSettings ? headerSettings.backdrop : null,
      ].filter(Boolean);
      holder.replaceChildren(...stepNodes);
      for (const node of stepNodes) {
        if (node === headerRow || node === progressRow) continue;
        slideIn(node, stepDir);
      }
      stepDir = "fwd";
      syncMainButton();
    }

    // Разбивает шаблон с ОДНИМ токеном на три куска (до/значение/после) — так «жирным» можно
    // пометить только подставленное значение, не весь текст (`.folded` — список пропущенных
    // шагов жирным, 30-UI-SPEC.md), без `innerHTML`/сборки разметки строкой.
    function splitTemplate(template, token, value) {
      const idx = String(template || "").indexOf(token);
      if (idx === -1) return [String(template || ""), "", ""];
      return [template.slice(0, idx), value, template.slice(idx + token.length)];
    }

    // Phase 30 (30-05, задача 2, A2-07, 30-UI-SPEC.md § «Обзор перед отправкой»): три группы
    // ответов + одна свёрнутая строка пропущенного необязательного — постоянное поведение новой
    // анкеты (решение оркестратора 12.09), собственных тумблеров у обзора нет.
    // Живой прогон 16.09 (п.3а): «Пропустить» на необязательном шаге пишет сервер-совместимый
    // плейсхолдер «-» (goSkip() выше, та же семантика, что в боте) — НЕ пустую строку, значит
    // общий `stepAnswered` (v != null && v !== "", контракт зафиксирован сторожем
    // test_form_screen_stepanswered_contract_unchanged, менять нельзя) считает такой шаг
    // отвеченным, и обзор показывал «12 из 12, 0 пропущено» вместо реальной картины. Сервер
    // сам уже не считает «-» ответом (`reg_engine.form_spec::has_answer`, `v not in (None, "",
    // "-")`) — здесь та же граница, но ТОЛЬКО для подсчёта/группировки обзора (окно вопросов
    // мастера и обзор точечной правки продолжают использовать `stepAnswered` как раньше,
    // «-» там — валидный сохранённый ответ, например «аллергий нет»).
    function reviewAnswered(spec, state) {
      return (spec.columns || [spec.column]).some((col) => {
        const v = state.value(col);
        return v != null && v !== "" && v !== "-";
      });
    }

    function drawReview() {
      setMainButton(null);
      const specs = state.specs;
      const groups = { about: [], study: [], event: [] };
      const skipped = [];
      for (const spec of specs) {
        if (!reviewAnswered(spec, state) && !spec.required) { skipped.push(spec); continue; }
        groups[reviewGroupOf(spec.key)].push(spec);
      }
      const filledCount = specs.length - skipped.length;

      function reviewRow(spec) {
        const isComposite = spec.kind === "composite" || spec.degraded_kind === "composite";
        if (isComposite) {
          return h("div", { class: "row stacked" },
            h("div", { text: labelText(spec.label) }),
            h("div", { class: "val filled", text: compositeSummary(spec) }),
          );
        }
        const hasDisplay = spec.display != null && spec.display !== "";
        const shown = hasDisplay ? spec.display : spec.value;
        return h("div", { class: "row" },
          h("div", { text: labelText(spec.label) }),
          h("div", { class: "val filled", text: shown != null ? String(shown) : "" }),
        );
      }

      function groupSection(groupKey, titleKey) {
        const items = groups[groupKey];
        if (!items.length) return null;
        return h("div", {},
          sectionTitle(h, formV2Text(titleKey)),
          h("div", { class: "rows compact" }, ...items.map(reviewRow)),
        );
      }

      let foldedRow = null;
      if (skipped.length) {
        const names = skipped.map((s) => labelText(s.label)).join(", ");
        const [before, list, after] = splitTemplate(formV2Text("review_skipped_prefix"), "{list}", names);
        foldedRow = h("button", { class: "folded", type: "button", onClick: () => {
          stepIndex = stepIndexFromKey(state.specs, skipped[0].key);
          drawStep();
        } },
          h("span", {}, h("span", { text: before }), h("b", { text: list }), h("span", { text: after })),
          h("span", { class: "opt", text: formV2Text("review_fill_action") || "" }),
        );
      }

      const [sBefore, sFilled, sMid1] = splitTemplate(formV2Text("review_summary"), "{filled}", String(filledCount));
      const midTemplate = sMid1;
      const [sMid, sTotal, sMid2] = splitTemplate(midTemplate, "{total}", String(specs.length));
      const [sTail, sSkipped, sEnd] = splitTemplate(sMid2, "{skipped}", String(skipped.length));
      const summaryLine = h("p", { class: "plate-sub" },
        h("span", { text: sBefore }), h("span", { text: sFilled }), h("span", { text: sMid }),
        h("span", { text: sTotal }), h("span", { text: sTail }), h("span", { text: sSkipped }),
        h("span", { text: sEnd }),
      );

      const plate = h("section", { class: "plate plate--form" },
        h("div", { class: "plate-row" }, h("span", { class: "plate-eyebrow", text: formV2Text("review_eyebrow") })),
        h("h1", { text: formV2Text("review_title") }),
        summaryLine,
      );

      const mainLabel = formV2Text("review_submit_button");
      const mainBtn = h("button", {
        class: "btn", type: "button", disabled: busy, "aria-label": mainLabel, onClick: submitForm,
      }, h("span", { text: mainLabel }), icon("arrow-right"));
      const footer = h("div", { class: "task-actions" }, mainBtn);

      holder.replaceChildren(...[
        plate,
        groupSection("about", "review_group_about"),
        groupSection("study", "review_group_study"),
        groupSection("event", "review_group_event"),
        foldedRow,
        footer,
      ].filter(Boolean));
      setMainButton(mainLabel, submitForm, { disabled: busy });
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
          say(failText(err), "warn");
          drawCurrent();
        } else if (!isAuthError(err)) {
          say(failText(err), "warn");
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
