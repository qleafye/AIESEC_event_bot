// Хаб делегата и менеджера (план 19.1-04, D-09 — делегат заперт на вариант C; D-10 — тот же
// компонент по умолчанию для менеджера, пока голосование команды не выбрало A/B/C) + одно-
// разовый привет-экран. Дом приложения при NAV_LAYOUT === "hub" (см. app.js).
//
// Делегатский состав плиток строится тем же visibleNav(), что и раскладки таб-бара.
// Дом менеджера — три режима: Работа, Настройки, Форум. Права по-прежнему проверяет сервер
// на каждом маршруте (T-19.1-14). Подсказки менеджера fail-soft (T-19.1-16).

import { visibleNav, NAV_ICONS, managerSettingsLaunchOn } from "../app.js";
import { icon } from "../icons.js";
import { confetti, countUp, haptic, stagger } from "../motion.js";
import {
  fileUrl, flatRow, sectionTitle, labelText, tile, noticeBox, ambassadorLinkBlock, formatCount, screenText,
} from "../ui.js";
import { unitFor } from "../units.js";
import { personNode } from "../person.js";

// Phase 30 (30-05, задача 3, A2-07): плита статуса заявки. Тумблер `reg_form_status_screen`
// решает СЕРВЕР (`status.status_screen_enabled`, `miniapp/routers/hub.py::hub_status`) —
// выключен -> клиент не видит ни одного нового поля, плита остаётся сегодняшней (не кликается).
// Квик 260922-wrg (задача 2, B-2): "returning" — тот же нейтральный тон, что "pending", своей
// CSS-палитры не заводим.
const STATUS_TONE = { pending: "accent", approved: "success", rejected: "danger", returning: "accent" };

// `motion.js::haptic` не знает про `reg_form_haptics` (та управляет ТОЛЬКО модулем анкеты,
// form_types.js::haptic — свой локальный хелпер с тем же принципом) — плита статуса живёт в
// hub.js, третьей копии детекта уровня не заводим, просто гейтим вызов флагом с сервера.
function statusHaptic(status) {
  if (status && status.haptics_enabled) haptic("light");
}

// Quick 260904-aup (UAT D11 + Q4): разделение ролей — СЕРВЕР (`me.show_onboarding`, `/app/api/me`)
// решает, кому вообще положено видеть привет-экран (не сотруднику и не делегату с уже поданной
// анкетой), localStorage ниже по-прежнему решает только «показывали ли уже НА ЭТОМ устройстве».
// Раньше гейта на сервере не было — сотрудник и делегат с одобренной анкетой видели экран
// заново на новом устройстве или после очистки localStorage.
const ONBOARDING_KEY = "aiesec_miniapp_onboarding_seen_v1";

function hasSeenOnboarding() {
  try {
    return localStorage.getItem(ONBOARDING_KEY) === "1";
  } catch (_) {
    return true; // приватный режим/недоступный localStorage — не блокируем хаб повторным экраном
  }
}

function markOnboardingSeen() {
  try {
    localStorage.setItem(ONBOARDING_KEY, "1");
  } catch (_) { /* см. hasSeenOnboarding — тихо игнорируем */ }
}

function sectionLabelsFromDom() {
  try {
    return JSON.parse(document.body.dataset.sectionLabels || "{}");
  } catch (_) {
    return {};
  }
}

// Шаги «как это работает» (D-06): `miniapp_onboarding_steps` — шаги через `;`, внутри шага
// заголовок и пояснение через ` — ` (перевод строки в подписи Telegram отправляет сообщение,
// поэтому не многострочный текст). Пустые куски и лишние пробелы менеджера не рождают строк.
function parseOnboardingSteps(raw) {
  return (raw || "")
    .split(";")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => {
      const sepIndex = s.indexOf(" — ");
      return sepIndex === -1
        ? { title: s, meta: "" }
        : { title: s.slice(0, sepIndex), meta: s.slice(sepIndex + 3) };
    });
}

function onboardingStepRow(h, step, index) {
  return flatRow(h, {
    leadText: String(index + 1).padStart(2, "0"),
    title: step.title,
    meta: step.meta || undefined,
  });
}

// ── привет-экран (D-09): показывается один раз, все тексты из реестра — герой на плите с
// паттерном (план 23.1-03, макет mockups/02-onboarding.png) и три шага «как это работает». ──
function renderOnboarding(root, ctx, onDone) {
  const { h, me } = ctx;
  const dark = document.documentElement.dataset.theme === "dark";
  const coverId = (dark && me.cover_dark_file_id) ? me.cover_dark_file_id : me.cover_file_id;

  const plate = h("section", { class: "plate plate--onboarding" });
  if (coverId) {
    const img = h("img", { class: "onboarding-cover", src: fileUrl(coverId), alt: "" });
    img.addEventListener("error", () => img.remove()); // обложки нет/не грузится — экран всё равно корректен
    plate.append(img);
  }
  plate.append(
    h("div", { class: "onboarding-hero", text: me.onboarding_hero || "" }),
    h("div", { class: "onboarding-rule" }),
    h("p", { class: "onboarding-slogan", text: me.onboarding_text || "" }),
  );

  const steps = parseOnboardingSteps(me.onboarding_steps);
  const stepsBlock = steps.length
    ? [
      sectionTitle(h, me.onboarding_steps_title || ""),
      h("div", { class: "flat-list flush onboarding-steps" }, ...steps.map((step, i) => onboardingStepRow(h, step, i))),
    ]
    : [];

  const button = h("button", {
    class: "btn", type: "button", text: me.onboarding_cta || "",
    onClick: () => { markOnboardingSeen(); onDone(); },
  });

  root.append(plate, ...stepsBlock, button);
}

// Строка приоритетного действия (D-06): статус-или-срок — то же правило, что было у
// deadlineRow (принято / срок вышел / до {deadline_short}), это не новый литерал. Ветка
// «срок вышел» остаётся первой и не меняется (Phase 32, 32-14, D-27) — предлог «до» ставится
// только при `has_deadline`, иначе печатается готовая подпись сервера (`item.deadline_text`).
function nextActionMeta(item) {
  if (item.status === "approved") return "принято";
  if (item.overdue) return "срок вышел";
  return item.has_deadline ? `до ${item.deadline_short}` : item.deadline_text;
}

function nextActionReward(item) {
  return item.status === "approved" && item.coins_awarded != null ? item.coins_awarded : item.coins;
}

function nextAction(h, navigate, item) {
  const reward = nextActionReward(item);
  return h("button", {
    class: "next-action", type: "button",
    onClick: () => navigate(`#/task/${item.id}`),
  },
    h("div", { class: "next-action-body" },
      h("div", { class: "next-action-title", text: item.title }),
      h("div", {
        class: "next-action-meta",
        text: [item.category_label, nextActionMeta(item)].filter(Boolean).join(" · "),
      }),
    ),
    h("div", { class: "next-action-reward" },
      h("b", { text: reward != null ? `+${reward}` : "" }),
      icon("coin"),
    ),
  );
}

// ── хаб делегата (D-09, вариант C — заперт, не часть голосования; переезд на плиту —
// план 23.1-03, макет mockups/01-hub.png) ────────────────────────────────────────────────
async function renderDelegateHub(root, ctx) {
  const { h, api, navigate } = ctx;
  const labels = sectionLabelsFromDom();
  const items = visibleNav().filter((item) => item.delegate);

  // Плита баланса — в DOM сразу, без ожидания сети (сегодняшнее поведение): число
  // докручивается countUp'ом, надзаголовок/единица/факты дозаполняются ответом /hub.
  // Квик 260911-6i9 (пункт 1): личность делегата — предсозданный пустой слот ПЕРВЫМ узлом
  // плиты, как и остальные слоты здесь, — ответ /profile приезжает позже первого кадра,
  // DOM-порядок плиты не должен зависеть от порядка ответов Promise.allSettled ниже.
  // Квик 12.09 (UI-аудит, пункт 11): пока личность/надзаголовок/единица не дозаполнены
  // ответами ниже, слоты несут скелетон-плейсхолдер вместо пустоты — снимается одним местом
  // сразу после Promise.allSettled, независимо от того, какая ручка отказала.
  const personSlot = h("div", { class: "skeleton skeleton--person" });
  const plateEyebrow = h("div", { class: "plate-eyebrow skeleton skeleton--eyebrow", text: "" });
  const plateBig = h("div", { class: "plate-big", text: "0" });
  const plateUnit = h("span", { class: "skeleton skeleton--unit", text: "" });
  const plateRow = h("div", { class: "plate-row" },
    plateBig, h("span", { class: "plate-coin" }, icon("coin")), plateUnit,
  );
  const factsSlot = h("div", {});
  root.append(h("section", { class: "plate plate--hub" }, personSlot, plateEyebrow, plateRow, factsSlot));

  // Phase 30 (30-05, задача 3, A2-07): короткая плита-ссылка на статус заявки — у ОДОБРЕННОГО
  // делегата сегодня нет вообще никакого сигнала «оплати до …»/«собирай монеты», кроме строки
  // в разделе «Анкета» ниже (`sectionRows["#/form"]`). Слот пуст, пока не пришёл ответ ниже —
  // тот же fail-soft приём, что у остальных слотов этой плиты (T-19.1-16).
  const statusSlot = h("div", {});
  root.append(statusSlot);

  // Приоритетное действие — заполняется только когда известны и текст надзаголовка (/hub),
  // и само задание (/tasks); до тех пор слот пуст, никакой пустой рамки не рисуется.
  const nextSlot = h("div", {});
  root.append(nextSlot);

  // Разделы — строятся сразу из visibleNav() (право по-прежнему проверяет сервер на каждом
  // маршруте, T-19.1-14); надзаголовок и значения справа дозаполняются ответами ниже
  // (fail-soft, T-19.1-16: не ответившая ручка даёт строку без значения, не пустой экран).
  const sectionsEyebrow = h("div", { class: "section-title", text: "" });
  const sectionsList = h("div", { class: "flat-list flush" });
  const sectionRows = {};
  for (const item of items) {
    const rowEl = flatRow(h, {
      icon: NAV_ICONS[item.hash],
      // D-04 (план 23.1-07): строка уже несёт Lucide-иконку слева — ведущий эмодзи подписи
      // реестра (labelText, ui.js) здесь не дублируется рядом с ней.
      title: labelText(labels[item.section] || item.section),
      value: item.hash === "#/form" ? (ctx.me.form_status_label || "") : "",
      valueCls: item.hash === "#/form" && ctx.me.form_status === "approved" ? "ok" : undefined,
      chevron: true,
      onClick: () => navigate(item.hash),
    });
    sectionRows[item.hash] = rowEl;
    sectionsList.append(rowEl);
  }
  root.append(sectionsEyebrow, sectionsList);

  // Приёмка 17.09 (п.1): постоянное место реф-ссылки — заполняется ниже, только если сервер
  // прислал `hub.referral` (то же правило видимости, что у кнопки чата «🔗 Моя реферальная
  // ссылка»: тумблер `menu_referral` + одобренная заявка). Пусто, пока ответ /hub не пришёл —
  // тот же fail-soft приём, что у остальных слотов этой плиты (T-19.1-16).
  const referralSlot = h("div", {});
  root.append(referralSlot);

  const anchorSlot = h("div", {});
  root.append(anchorSlot);

  const setSectionValue = (hash, value, cls) => {
    const valueEl = sectionRows[hash]?.querySelector(".flat-row-value");
    if (!valueEl) return;
    valueEl.textContent = value;
    if (cls) valueEl.classList.add(cls);
  };

  // Счётчики плиты и строк — фоновые: раздел, выключенный чекбоксом, отвечает 403 section_off,
  // и без quiet ядро увело бы всю главную в «Нет доступа». Выключенный раздел не зовём вовсе.
  // /hub — без quiet: его 403 delegate_gate (прошлый сезон) обязан показать свой экран.
  const sec = (name) => Boolean(ctx.me.sections && ctx.me.sections[name]);
  const skip = Promise.reject(new Error("section_off"));
  skip.catch(() => {});
  const [balanceR, historyR, profileR, tasksR, hubR, statusR] = await Promise.allSettled([
    sec("coins") ? api("/coins/balance", { quiet: true }) : skip,
    sec("coins") ? api("/coins/history?offset=0&limit=1", { quiet: true }) : skip,
    sec("profile") ? api("/profile", { quiet: true }) : skip,
    sec("tasks") ? api("/tasks?offset=0&limit=2", { quiet: true }) : skip,
    api("/hub"),
    api("/hub/status"),
  ]);

  // Снять скелетон одним местом, до разбора результатов: отказ любой ручки не должен
  // оставить вечный плейсхолдер вместо пустой строки.
  personSlot.classList.remove("skeleton", "skeleton--person");
  plateEyebrow.classList.remove("skeleton", "skeleton--eyebrow");
  plateUnit.classList.remove("skeleton", "skeleton--unit");

  if (balanceR.status === "fulfilled") {
    const bal = balanceR.value;
    countUp(plateBig, 0, bal.balance || 0);
    if (bal.rank != null) {
      plateRow.append(h("span", {
        class: "chip sec",
        text: bal.participants ? `${bal.rank}-й из ${bal.participants}` : `${bal.rank}-й`,
      }));
    }
    setSectionValue("#/leaderboard", bal.rank == null ? "пока без места" : `ты ${bal.rank}-й`);
  }
  if (historyR.status === "fulfilled") {
    setSectionValue("#/coins", opsCount(historyR.value.total));
  }
  if (profileR.status === "fulfilled") {
    const p = profileR.value;
    // Квик 260911-6i9 (пункт 1): личность плиты из уже выполняемого запроса /profile —
    // второго запроса к серверу не заводим. Отказ ручки (ветка "rejected" выше по allSettled)
    // оставляет слот пустым — плита остаётся рабочей, просто без имени.
    personSlot.append(personNode(h, {
      avatarUrl: p.avatar_url, initials: p.initials, name: p.display_name,
    }, { compact: true }));
    if (p.payment_status_label) {
      // D-08: пусто = модуль оплаты выключен — строка «Профиль» остаётся без значения.
      setSectionValue("#/profile", p.payment_status_label);
    }
  }
  if (tasksR.status === "fulfilled") {
    setSectionValue("#/tasks", `${tasksR.value.total} активных`);
  }

  // Phase 30 (30-05, задача 3): плита видна, только пока есть активная заявка (сервер прислал
  // `tile_text`) И включён `reg_form_status_screen` (`status_screen_enabled`) — выключенный
  // тумблер оставляет слот пустым, делегат не видит ничего нового (сегодняшнее поведение).
  if (statusR.status === "fulfilled") {
    const status = statusR.value;
    // Квик 260922-wrg (задача 2, B-2): "returning" не несёт status_screen_enabled (сервер его
    // всегда шлёт false — у возвращенца нет расширенного экрана статуса), но плита всё равно
    // положена — тап ведёт прямо на #/form, а не на #/status.
    const isReturning = status && status.status === "returning";
    if (status && status.tile_text && (status.status_screen_enabled || isReturning)) {
      const tone = STATUS_TONE[status.status] || "accent";
      const openStatus = () => { statusHaptic(status); navigate(isReturning ? "#/form" : "#/status"); };
      statusSlot.append(h("button", {
        class: `next-action status-tile status-${tone}`, type: "button",
        "aria-label": status.tile_text, onClick: openStatus,
      },
        icon(status.status === "approved" ? "check" : status.status === "rejected" ? "alert-triangle"
          : isReturning ? "refresh-cw" : "clock-4"),
        h("div", { class: "next-action-body" }, h("div", { class: "next-action-title", text: status.tile_text })),
        h("span", { class: "flat-row-chev" }, icon("chevron-right")),
      ));
    }
  }

  if (hubR.status === "fulfilled") {
    const hub = hubR.value;
    plateEyebrow.textContent = hub.balance_eyebrow || "";
    // Число на плите — баланс: «1 балл», «22 балла» (без баланса — подпись как есть).
    const shown = balanceR.status === "fulfilled" ? (balanceR.value.balance || 0) : null;
    plateUnit.textContent = shown == null ? (hub.balance_unit || "") : unitFor(shown, hub.balance_unit);
    // Идея №4 бэклога чек-ина: «✅ Ты отмечен» — та же плита фактов, что tasks_fact/days_fact,
    // третьей строкой (сервер уже решил видимость: `checkin_qr_enabled` + отметка есть,
    // `miniapp/routers/hub.py::_checkin_status_fact`).
    if (hub.tasks_fact || hub.days_fact || hub.checkin_status_fact) {
      factsSlot.append(
        h("hr", { class: "plate-rule" }),
        h("div", { class: "plate-facts" },
          hub.tasks_fact ? h("span", { text: hub.tasks_fact }) : null,
          hub.days_fact ? h("span", { text: hub.days_fact }) : null,
          hub.checkin_status_fact ? h("span", { text: hub.checkin_status_fact }) : null,
        ),
      );
    }
    sectionsEyebrow.textContent = hub.sections_eyebrow || "";
    // Приёмка 17.09 (п.1): постоянное место реф-ссылки — та же плита-компонент, что финальный
    // экран анкеты (`ui.js::ambassadorLinkBlock`), второй копии рендера нет. `hub.referral` —
    // `null`, когда тумблер `menu_referral` выключен для города делегата (кнопки в чате тоже
    // нет), поэтому слот тогда остаётся пустым.
    if (hub.referral) {
      const { el: referralNotice, say: sayReferral } = noticeBox(h);
      referralSlot.append(
        sectionTitle(h, hub.referral.label || ""),
        ambassadorLinkBlock(h, hub.referral, { haptic, say: sayReferral }),
        referralNotice,
      );
    }
    if (tasksR.status === "fulfilled" && tasksR.value.items.length) {
      nextSlot.append(
        sectionTitle(h, hub.next_eyebrow || ""),
        nextAction(h, navigate, tasksR.value.items[0]),
      );
    }
    if (hub.event_dates || hub.event_place) {
      anchorSlot.append(h("div", { class: "screen-anchor" },
        icon("calendar"),
        h("div", {},
          hub.event_dates ? h("div", { class: "screen-anchor-title", text: hub.event_dates }) : null,
          hub.event_place ? h("div", { class: "screen-anchor-sub", text: hub.event_place }) : null,
        ),
      ));
    }
  }
}

// ── хаб из одних плиток: не-делегат с доступной анкетой (D-24, gap closure фазы 21) ────
// Незарегистрированный/pending/rejected видит плитку «Анкета» (form_access), но остальные
// делегатские ручки (/coins|/profile|/tasks) отвечают ему 403 — герой и «Ближайший
// дедлайн» не рисуются, запросов к ним нет. Подпись плитки анкеты — статус с сервера.
async function renderTilesOnlyHub(root, ctx, items) {
  const { h, api, navigate } = ctx;
  const labels = sectionLabelsFromDom();

  // Плита «Анкета на проверке / заявка отклонена» (UAT D3): запрашивается только когда
  // статус вообще может нести текст — approved/none/draft получают heading: None и ручку
  // можно не звать вовсе. Отказ ручки -> плиты нет, плитки на месте (fail-soft, T-19.1-16).
  if (
    ctx.me.form_status === "pending" || ctx.me.form_status === "rejected"
    || ctx.me.form_status === "approved" || ctx.me.form_status === "returning"
  ) {
    try {
      const status = await api("/hub/status");
      // Одобренная заявка несёт текст ТОЛЬКО в новых полях (`tile_text`/`title`) — `heading`/
      // `body` у неё пустые и до этого плана (approved никогда не показывал строку статуса).
      if (status && (status.heading || status.tile_text)) {
        const plate = h("section", { class: "plate plate--hub" },
          h("h1", { text: status.heading || status.tile_text || "" }),
          h("hr", { class: "plate-rule" }),
          h("p", { class: "plate-sub", text: status.body || "" }),
          // Quick 260904-liz: строка причины отказа — второй узел, создаётся ТОЛЬКО когда
          // сервер прислал reason_line (нет причины/старый отказ -> None -> узла нет вовсе,
          // «Причина: None» на экране невозможна). Подстановка уже сделана на сервере
          // (miniapp/routers/hub.py) — здесь только textContent, никакого форматирования.
          status.reason_line ? h("p", { class: "plate-sub", text: status.reason_line }) : null,
        );
        // Phase 30 (30-05, задача 3): тумблер выключен -> ниже как раньше (инлайн-кнопка
        // «Поправить»/«Изменить», карточка НЕ кликается) — сегодняшнее поведение без единого
        // изменения. Тумблер включён -> вся карточка становится ссылкой на #/status (там уже
        // есть та же кнопка-действие), второй, дублирующей кнопки внутри плиты не остаётся.
        if (status.status_screen_enabled) {
          plate.classList.add(`status-${STATUS_TONE[status.status] || "accent"}`, "clickable");
          plate.setAttribute("role", "button");
          plate.setAttribute("tabindex", "0");
          plate.setAttribute("aria-label", status.tile_text || status.heading || "");
          const openStatus = () => { statusHaptic(status); navigate("#/status"); };
          plate.addEventListener("click", openStatus);
          plate.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openStatus(); }
          });
          plate.append(h("span", { class: "flat-row-chev" }, icon("chevron-right")));
        } else if ((status.status === "rejected" || status.status === "returning") && status.cta_text) {
          // Квик 260922-wrg (задача 2, B-2/A-5): тот же приём, что у rejected без расширенного
          // экрана — кнопка ведёт прямо на #/form (у rejected при deny cta_text уже None,
          // сервер убрал кнопку сам, см. hub_status).
          plate.append(h("button", {
            class: "btn", type: "button", text: status.cta_text,
            onClick: () => navigate("#/form"),
          }));
        }
        root.append(plate);
        if (status.event_dates || status.event_place) {
          root.append(h("div", { class: "screen-anchor" },
            icon("calendar"),
            h("div", {},
              status.event_dates ? h("div", { class: "screen-anchor-title", text: status.event_dates }) : null,
              status.event_place ? h("div", { class: "screen-anchor-sub", text: status.event_place }) : null,
            ),
          ));
        }
      }
    } catch (_) {
      // fail-soft (T-19.1-16): плиты нет, плитки всё равно рисуются ниже
    }
  }

  const tiles = h("div", { class: "tiles" });
  for (const item of items) {
    tiles.append(tile(h, {
      onClick: () => navigate(item.hash),
      iconName: NAV_ICONS[item.hash],
      // D-04: плитка уже несёт Lucide-иконку слева — ведущий эмодзи подписи реестра здесь
      // не дублируется рядом с ней (тот же приём, что renderDelegateHub чуть выше).
      label: labelText(labels[item.section] || item.section),
      meta: item.hash === "#/form" ? (ctx.me.form_status_label || "") : "",
    }));
  }
  stagger(tiles); // квик 260915-4mw (ANIM-05): плитки хаба появляются лесенкой ≤8 подряд
  root.append(tiles);
}

// ── хаб менеджера: три режима (Работа, Настройки, Форум), не группы разделов ─────────────

// Плитка «📊 Дашборд» (quick 260903): открывает веб-дашборд во внешнем браузере — НЕ раздел
// NAV (нет hash/секции), поэтому не строится через tile()/visibleNav(). Адрес и подпись
// приходят с сервера (`/app/api/me` -> dashboard_url/dashboard_tile_label): адрес — деплойный
// `cfg.public_url` (D-05/D-19, тот же источник, что у кнопки «🌐 Открыть дашборд» в боте),
// пустой -> плитки нет вовсе. Тот же приём, что у остальных плиток группы «data» — в
// Telegram-клиенте открываем через WebApp SDK (tg.openLink), вне Telegram — обычной вкладкой.
function openDashboardLink(tg, url) {
  if (tg && typeof tg.openLink === "function") tg.openLink(url);
  else window.open(url, "_blank", "noopener");
}

function dashboardTile(h, tg, me) {
  if (!me.dashboard_url) return null;
  return h("button", {
    type: "button",
    class: "tile",
    onClick: () => openDashboardLink(tg, me.dashboard_url),
  },
    icon("link"),
    h("b", { text: me.dashboard_tile_label || "" }),
  );
}

// Счётчик истории баллов склоняется по числу: шаблон из реестра
// (miniapp_coins_ops_count_text) через formatCount, как у остальных счётчиков; без шаблона — число.
function opsCount(n) {
  return formatCount(screenText("coins_ops_count") || "{n}", Number(n) || 0);
}

const MANAGER_MODE_KEY = "aiesec_miniapp_manager_mode_v1";

function storedManagerMode() {
  try {
    const value = localStorage.getItem(MANAGER_MODE_KEY);
    if (value === "work" || value === "settings" || value === "forum") return value;
  } catch (_) { /* приватный режим — как пустой ключ */ }
  return "work";
}

function setManagerMode(mode) {
  try { localStorage.setItem(MANAGER_MODE_KEY, mode); }
  catch (_) { /* приватный режим */ }
}

// Пин на запуск: query выбирает режим один раз и не пишет ключ. Явный тап сегмента — пишет.
let managerModePin = null;

function currentManagerMode() {
  if (managerModePin == null) {
    managerModePin = managerSettingsLaunchOn() ? "settings" : storedManagerMode();
  }
  return managerModePin;
}

function pickManagerMode(mode) {
  if (mode === currentManagerMode()) return;
  managerModePin = mode;
  setManagerMode(mode);
}

let managerViewPin = null;

function managerViewForced() {
  if (managerViewPin == null) managerViewPin = managerSettingsLaunchOn();
  return managerViewPin === true;
}

function dismissManagerViewForce() {
  managerViewPin = false;
}

let seasonRecapCelebrated = false;

async function renderManagerHub(root, ctx, opts = {}) {
  // opts оставлен: ветка анкеты и переключатель роли по-прежнему передают skipHero.
  void opts;
  const { h, api, navigate, me } = ctx;
  const hasCap = (cap) => Array.isArray(me && me.caps) && me.caps.includes(cap);

  // Квик 260915-4mu: плитка мастера первой настройки — первым блоком экрана, ПЕРЕД
  // подсказками ниже. Решение «показывать/нет» и текст прогресса считает сервер
  // (`show_tile`/`texts`, `GET /admin/setup`), хаб только рисует. Fail-soft — тот же приём,
  // что у `/admin/settings/hints` (403 у делегата без права `settings` даёт хаб без плитки,
  // не падение экрана).
  // Без права «settings» (волонтёр входа, модератор) оба запроса ниже не делаем вовсе: сервер
  // ответил бы 403, а ядро нарисовало бы «Нет доступа» поверх главной. quiet — страховка на
  // случай выключенного раздела настроек: тот же 403, но экран не трогаем.
  const canSettings = Array.isArray(me && me.caps) && me.caps.includes("settings");
  let setup = null;
  try {
    setup = canSettings ? await api("/admin/setup", { quiet: true }) : null;
  } catch (_) {
    setup = null;
  }
  if (setup && setup.show_tile) {
    const setupTiles = h("div", { class: "tiles" });
    setupTiles.append(tile(h, {
      onClick: () => navigate("#/setup"),
      iconName: "sparkles",
      label: labelText(setup.texts.tile_label),
      meta: (setup.texts.progress_note_text || "")
        .replace("{done}", String(setup.done_count)).replace("{total}", String(setup.total)),
    }));
    root.append(setupTiles);
  }

  // Подсказка про незаданную «🗓 Дата отсчёта до форума» (quick 260903, D-06): текст и решение
  // «показывать/нет» считает сервер — hub.js только рисует. Отказ ручки даёт хаб без строки,
  // а не падение экрана. Строка про настройку, не про очередь.
  let countdown = null;
  let quietQueue = null;
  try {
    const hints = canSettings ? await api("/admin/settings/hints", { quiet: true }) : null;
    countdown = hints && hints.countdown ? hints.countdown : null;
    // Quick 260904-dq1: «в очереди: N» — тот же fail-soft приём, что countdown выше; текст
    // считает сервер, hub.js только рисует строку, когда она непустая.
    quietQueue = hints && hints.quiet_queue ? hints.quiet_queue : null;
  } catch (_) {
    countdown = null;
    quietQueue = null;
  }
  if (countdown) {
    root.append(h("div", { class: "flat-list" },
      flatRow(h, {
        icon: "calendar",
        title: countdown.text,
        chevron: true,
        onClick: () => navigate(countdown.hash),
      }),
    ));
  }
  if (quietQueue) {
    root.append(h("div", { class: "flat-list" },
      flatRow(h, {
        icon: "clock",
        title: quietQueue.text,
        chevron: true,
        onClick: () => navigate(quietQueue.hash),
      }),
    ));
  }

  // Карточка сезона — между подсказками и сегментом. Список режимов её не ждёт.
  if (hasCap("stats")) {
    const card = h("div", { class: "card" });
    const grid = h("div", { class: "stats" });
    grid.style.gap = "var(--sp-4)";
    const figureFor = (label, note) => {
      const figure = h("div", { class: "home-figure", text: "…" });
      grid.append(h("div", {},
        figure,
        h("div", { class: "label-role", text: label }),
        note ? h("div", { class: "label-role", text: note }) : null,
      ));
      return figure;
    };
    const applications = figureFor("Заявки");
    const arrived = figureFor("Пришедшие");
    const participants = figureFor("Игроки", "по всем городам");
    const questions = figureFor("Вопросы");
    const hide = h("button", {
      type: "button",
      class: "muted",
      text: "Скрыть итог",
      onClick: () => {
        card.remove();
        api("/stats/season-recap/dismiss", { method: "POST", quiet: true }).catch(() => {});
      },
    });
    hide.style.marginTop = "var(--sp-4)";
    hide.style.background = "transparent";
    hide.style.border = "0";
    hide.style.padding = "0";
    hide.style.minHeight = "var(--tap-min)";
    hide.style.font = "inherit";
    hide.style.cursor = "pointer";
    card.append(h("h1", { text: "Сезон прошёл" }), grid, hide);
    root.append(card);
    api("/stats/season-recap", { quiet: true }).then((data) => {
      if (!card.isConnected) return;
      if (!data || data.show !== true) {
        card.remove();
        return;
      }
      const asFigure = (value) => (value == null ? "…" : String(value));
      applications.textContent = asFigure(data.applications);
      arrived.textContent = asFigure(data.arrived);
      participants.textContent = asFigure(data.participants);
      questions.textContent = asFigure(data.questions);
      if (!seasonRecapCelebrated) {
        seasonRecapCelebrated = true;
        confetti(card);
      }
    }).catch(() => {
      if (card.isConnected) card.remove();
    });
  }

  // Входы папки «День форума»: расписание без отдельной капы (null), остальные — право ручки.
  const forumDayEntryCaps = ["checkin", null, "checkin", "moderate_reg", "checkin"];
  const forumDayVisible = forumDayEntryCaps.some((cap) => cap == null || hasCap(cap));
  const workRows = [
    { cap: "moderate_reg", icon: "clipboard-list", title: "Отбор заявок", hash: "#/applications" },
    { cap: "checkin", icon: "camera", title: "Сканер QR", hash: "#/scanner" },
    {
      cap: null, icon: "calendar", title: "Расписание", hash: "#/program",
      meta: "Посмотреть сессии, не переставлять их.",
    },
    { folder: "forum-day", icon: "map-pin", title: "День форума", hash: "#/forum-day" },
    { cap: "moderate_reg", icon: "help-circle", title: "Вопросы делегатов", hash: "#/questions" },
    { cap: "broadcast", icon: "mail", title: "Рассылка", hash: "#/chat/broadcast" },
    { cap: null, icon: "coins", title: "Баллы и магазин", hash: "#/coins-shop" },
    { cap: "stats", icon: "bar-chart-2", title: "Цифры", hash: "#/figures" },
  ];
  const settingsRows = [
    { cap: "settings", icon: "smartphone", title: "Кнопки меню делегата", hash: "#/settings/row-menu" },
    { cap: "settings", icon: "pen-line", title: "Тексты делегату", hash: "#/settings/row-texts" },
    { cap: "settings", icon: "pencil", title: "Вопросы анкеты", hash: "#/settings/row-questions" },
    { cap: "settings", icon: "file-text", title: "Таблица и формы", hash: "#/sheets-forms" },
    { cap: "settings", icon: "clock", title: "Запись и напоминания", hash: "#/settings/row-reminders" },
    { cap: "settings", icon: "wallet", title: "Оплата и согласия", hash: "#/settings/row-pay" },
    { cap: "settings", icon: "message-circle", title: "Чат города", hash: "#/settings/row-chat" },
    { cap: "settings", icon: "user", title: "Амбассадоры", hash: "#/settings/row-amb" },
    { cap: "settings", icon: "settings", title: "Редкое", hash: "#/settings/row-rare" },
  ];
  const forumTiles = [
    { cap: "checkin", icon: "camera", title: "Сканер QR", hash: "#/scanner", meta: "Отметка на входе" },
    { cap: null, icon: "calendar", title: "Расписание", hash: "#/program", meta: "Посмотреть сессии" },
    { cap: "checkin", icon: "check-circle-2", title: "Отметки", hash: "#/arrivals", meta: "Кто на площадке" },
    { cap: "moderate_reg", icon: "alert-triangle", title: "SOS", hash: "#/chat/sos", meta: "Пока в чате" },
    { cap: "checkin", icon: "briefcase", title: "Находки", hash: "#/chat/lost", meta: "Пока в чате" },
    { cap: "checkin", icon: "wallet", title: "Магазин", hash: "#/scanner", meta: "Стойка выдачи" },
    { cap: "moderate_game", icon: "coins", title: "Баллы", hash: "#/admin-coins" },
  ];
  const visible = (item) => {
    if (item.folder === "forum-day") return forumDayVisible;
    if (!item.cap) return true;
    return hasCap(item.cap);
  };
  const mode = currentManagerMode();
  const seg = (id, label) => h("button", {
    class: `hub-seg-btn${mode === id ? " active" : ""}`,
    type: "button",
    role: "tab",
    "aria-selected": mode === id ? "true" : "false",
    text: label,
    onClick: () => {
      if (mode === id) return;
      pickManagerMode(id);
      root.replaceChildren();
      renderHub(root, ctx);
    },
  });
  root.append(h("div", {
    class: "hub-seg",
    role: "tablist",
    "aria-label": "Режим менеджера",
  }, seg("work", "Работа"), seg("settings", "Настройки"), seg("forum", "Форум")));

  const source = mode === "settings" ? settingsRows : mode === "forum" ? forumTiles : workRows;
  const items = source.filter(visible);
  if (!items.length) {
    root.append(
      h("h1", { text: "В этом режиме пусто" }),
      h("p", { text: "Для вашей роли здесь нет строк. Если дело пропало, попросите того, кто выдаёт доступ, проверить права." }),
    );
    return;
  }
  if (mode === "forum") {
    const tiles = h("div", { class: "tiles" });
    for (const item of items) {
      tiles.append(tile(h, {
        onClick: () => navigate(item.hash),
        iconName: item.icon,
        label: item.title,
        meta: item.meta || "",
      }));
    }
    stagger(tiles);
    root.append(tiles);
    return;
  }
  root.append(h("div", { class: "flat-list" }, items.map((item) => flatRow(h, {
    icon: item.icon,
    title: item.title,
    meta: item.meta,
    chevron: true,
    onClick: () => navigate(item.hash),
  }))));
}

// Выбор вида хаба для делегата-менеджера (владелец 02.09: «неудобно, когда всё на одном
// экране») — сегмент-переключатель сверху, выбор живёт в localStorage этого устройства.
const HUB_MODE_KEY = "aiesec_miniapp_hub_mode_v1";

function hubMode() {
  try { return localStorage.getItem(HUB_MODE_KEY) === "manager" ? "manager" : "delegate"; }
  catch (_) { return "delegate"; }
}

function setHubMode(mode) {
  try { localStorage.setItem(HUB_MODE_KEY, mode); } catch (_) { /* приватный режим */ }
}

function modeSwitch(h, active, onPick) {
  const seg = (mode, label) => h("button", {
    class: `hub-seg-btn${active === mode ? " active" : ""}`,
    type: "button", text: label,
    onClick: () => { if (active !== mode) onPick(mode); },
  });
  return h("div", { class: "hub-seg", role: "tablist" },
    seg("delegate", "Делегат"), seg("manager", "Менеджер"));
}

async function renderHub(root, ctx) {
  // Менеджер, прошедший регистрацию делегатом (наш обычный случай), получает ОБА вида:
  // переключатель «Делегат | Менеджер» сверху (владелец 02.09), менеджерский вид — та же
  // карта разделов, что корень /admin (ADMIN-IA-04). До 02.09 is_delegate прятал менеджерскую
  // часть целиком — «Проверка сдач»/«Статистика» были недостижимы (находка приёмки 19-10).
  // У чистого делегата и чистого менеджера переключателя нет — рисуется единственный вид.
  const isDelegate = Boolean(ctx.me.is_delegate);
  const hasManager = visibleNav().some((item) => !item.delegate);
  const delegateItems = visibleNav().filter((item) => item.delegate);
  const forceManager = managerViewForced();
  // Не-делегат с доступной анкетой (D-24): плитка анкеты над менеджерскими разделами (если
  // они есть) — незарегистрированный менеджер не теряет ни того, ни другого.
  if (!isDelegate && delegateItems.length) {
    await renderTilesOnlyHub(root, ctx, delegateItems);
    if (hasManager || forceManager) await renderManagerHub(root, ctx, { skipHero: true });
    return;
  }
  if (isDelegate && (hasManager || forceManager)) {
    const mode = forceManager ? "manager" : hubMode();
    if (hasManager) {
      root.append(modeSwitch(ctx.h, mode, (next) => {
        dismissManagerViewForce();
        setHubMode(next);
        root.replaceChildren();
        renderHub(root, ctx);
      }));
    }
    if (mode === "manager") await renderManagerHub(root, ctx, { skipHero: false });
    else await renderDelegateHub(root, ctx);
  } else if (isDelegate) {
    await renderDelegateHub(root, ctx);
  } else {
    await renderManagerHub(root, ctx);
  }
}

export async function render(root, params, ctx) {
  const { setMainButton } = ctx;
  setMainButton(null); // хаб — дом; MainButton здесь не показывается (D-10 вариант C)

  if (ctx.me.show_onboarding && !hasSeenOnboarding()) {
    renderOnboarding(root, ctx, () => {
      root.replaceChildren();
      renderHub(root, ctx);
    });
    return;
  }
  await renderHub(root, ctx);
}
