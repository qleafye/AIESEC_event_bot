// Хаб делегата и менеджера (план 19.1-04, D-09 — делегат заперт на вариант C; D-10 — тот же
// компонент по умолчанию для менеджера, пока голосование команды не выбрало A/B/C) + одно-
// разовый привет-экран. Дом приложения при NAV_LAYOUT === "hub" (см. app.js).
//
// Состав плиток строится ТЕМ ЖЕ visibleNav(), что и раскладки таб-бара/верхних табов —
// выключенный чекбоксом раздел не даёт плитку, права по-прежнему проверяет сервер на каждом
// маршруте (T-19.1-14). Счётчики менеджера — существующие списочные ручки /app/api/* с
// минимальным limit, каждый запрос fail-soft по отдельности (T-19.1-16): не ответившая ручка
// даёт плитку без цифры, а не пустой экран.

import { visibleNav, NAV_ICONS, SECTION_GROUPS } from "../app.js";
import { icon } from "../icons.js";
import { countUp } from "../motion.js";
import { flatRow, sectionTitle, labelText } from "../ui.js";

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

function tile(h, navigate, { hash, iconName, label, meta }) {
  return h("button", {
    type: "button",
    class: "tile",
    onClick: () => navigate(hash),
  },
    iconName ? icon(iconName) : null,
    h("b", { text: label }),
    h("small", { text: meta }),
  );
}

function daysSince(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  if (!m) return null;
  const submittedAt = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const days = Math.max(0, Math.floor((Date.now() - submittedAt) / 86400000));
  return days;
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
    const img = h("img", { class: "onboarding-cover", src: `/app/api/file/${coverId}`, alt: "" });
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
// deadlineRow (принято / срок вышел / до {deadline_short}), это не новый литерал.
function nextActionMeta(item) {
  if (item.status === "approved") return "принято";
  return item.overdue ? "срок вышел" : `до ${item.deadline_short}`;
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
  const plateEyebrow = h("div", { class: "plate-eyebrow", text: "" });
  const plateBig = h("div", { class: "plate-big", text: "0" });
  const plateUnit = h("span", { text: "" });
  const plateRow = h("div", { class: "plate-row" },
    plateBig, h("span", { class: "plate-coin" }, icon("coin")), plateUnit,
  );
  const factsSlot = h("div", {});
  root.append(h("section", { class: "plate plate--hub" }, plateEyebrow, plateRow, factsSlot));

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

  const anchorSlot = h("div", {});
  root.append(anchorSlot);

  const setSectionValue = (hash, value, cls) => {
    const valueEl = sectionRows[hash]?.querySelector(".flat-row-value");
    if (!valueEl) return;
    valueEl.textContent = value;
    if (cls) valueEl.classList.add(cls);
  };

  const [balanceR, historyR, profileR, tasksR, hubR] = await Promise.allSettled([
    api("/coins/balance"),
    api("/coins/history?offset=0&limit=1"),
    api("/profile"),
    api("/tasks?offset=0&limit=2"),
    api("/hub"),
  ]);

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
    setSectionValue("#/coins", `${historyR.value.total} операций`);
  }
  if (profileR.status === "fulfilled" && profileR.value.payment_status_label) {
    // D-08: пусто = модуль оплаты выключен — строка «Профиль» остаётся без значения.
    setSectionValue("#/profile", profileR.value.payment_status_label);
  }
  if (tasksR.status === "fulfilled") {
    setSectionValue("#/tasks", `${tasksR.value.total} активных`);
  }

  if (hubR.status === "fulfilled") {
    const hub = hubR.value;
    plateEyebrow.textContent = hub.balance_eyebrow || "";
    plateUnit.textContent = hub.balance_unit || "";
    if (hub.tasks_fact || hub.days_fact) {
      factsSlot.append(
        h("hr", { class: "plate-rule" }),
        h("div", { class: "plate-facts" },
          hub.tasks_fact ? h("span", { text: hub.tasks_fact }) : null,
          hub.days_fact ? h("span", { text: hub.days_fact }) : null,
        ),
      );
    }
    sectionsEyebrow.textContent = hub.sections_eyebrow || "";
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
  const { h, navigate } = ctx;
  const labels = sectionLabelsFromDom();
  const tiles = h("div", { class: "tiles" });
  for (const item of items) {
    tiles.append(tile(h, navigate, {
      hash: item.hash,
      iconName: NAV_ICONS[item.hash],
      label: labels[item.section] || item.section,
      meta: item.hash === "#/form" ? (ctx.me.form_status_label || "") : "",
    }));
  }
  root.append(tiles);
}

// ── хаб менеджера (D-10, вариант C по умолчанию до голосования) ─────────────────────────
const MANAGER_FETCHERS = {
  "#/review": (api) => api("/review/next?offset=0"),
  "#/admin-tasks": (api) => api("/admin/tasks?archived=0&offset=0&limit=1"),
  "#/admin-coins": (api) => api("/admin/coins?offset=0&limit=1"),
  "#/stats": (api) => api("/stats/game"),
  "#/settings": (api) => api("/admin/settings"),
};

function applyManagerTileData(hash, data, tileEl, hero) {
  const small = tileEl.querySelector("small");
  if (hash === "#/review") {
    const remaining = data.empty ? 0 : data.remaining;
    small.textContent = remaining ? `${remaining} на очереди` : "очередь пуста";
    if (!hero) return;
    countUp(hero.querySelector(".big"), 0, remaining || 0);
    const days = !data.empty && data.submission ? daysSince(data.submission.submitted_at) : null;
    hero.querySelector(".lbl").textContent = remaining
      ? `сдач на проверке${days != null ? ` · старейшая ${days} дн.` : ""}`
      : "сдач на проверке нет";
  } else if (hash === "#/admin-tasks") {
    small.textContent = `${data.active_count} активных · ${data.archived_count} в архиве`;
  } else if (hash === "#/admin-coins") {
    small.textContent = `${data.total} операций`;
  } else if (hash === "#/stats") {
    small.textContent = `${data.participants} участников`;
  } else if (hash === "#/settings") {
    small.textContent = `${data.length} настроек`;
  }
}

async function renderManagerHub(root, ctx, opts = {}) {
  // opts.skipHero — ветка делегата-менеджера (renderHub): плитки разделов дорисовываются ПОД
  // делегатским хабом, а большой герой «сдач на проверке» не дублирует делегатского героя
  // с монетами — счётчик очереди остаётся в подписи плитки «Проверка сдач».
  const { h, api, navigate } = ctx;
  const labels = sectionLabelsFromDom();
  const items = visibleNav().filter((item) => !item.delegate);
  const knownGroups = new Set(SECTION_GROUPS.map(([token]) => token));
  // Плитку без раздела (или с незнакомым токеном) терять нельзя: она уходит в хвост, под
  // «🔧 Управление». Лежать не на своём месте — плохо, исчезнуть с экрана молча — хуже.
  const groupOf = (item) => (knownGroups.has(item.group) ? item.group : "manage");

  // Герой считает очередь проверки сдач. Без плитки «#/review» (гейма выключена или нет
  // права) заполнять его нечем — он навсегда застыл бы на «0 / сдач на проверке» и врал.
  // ИНВАРИАНТ: герой существует ровно тогда, когда среди плиток есть «#/review» —
  // applyManagerTileData трогает hero только в этой ветке, поэтому null ниже безопасен.
  const hero = !opts.skipHero && items.some((item) => item.hash === "#/review")
    ? h("section", { class: "hero hero-flat" },
      h("div", { class: "big", text: "0" }),
      h("div", { class: "lbl", text: "сдач на проверке" }),
    )
    : null;
  if (hero) root.append(hero);

  // Хаб = та же карта админки, что корень /admin (Phase 20, ADMIN-IA-04, D-05): те же восемь
  // разделов, те же подписи, тот же порядок. Раздел без единой видимой плитки не рисуется
  // вовсе — ни заголовка, ни пустого контейнера (T-20-06: менеджер без права не узнаёт с
  // экрана о существовании чужого раздела). Подпись самой плитки по-прежнему из реестра
  // (labels[item.section]), а не из SECTION_GROUPS — это разные вещи.
  const tileEls = {};
  for (const [token, label] of SECTION_GROUPS) {
    const groupItems = items.filter((item) => groupOf(item) === token);
    if (!groupItems.length) continue;
    const tiles = h("div", { class: "tiles" });
    for (const item of groupItems) {
      const el = tile(h, navigate, {
        hash: item.hash,
        iconName: NAV_ICONS[item.hash],
        label: labels[item.section] || item.section,
        meta: "…",
      });
      tileEls[item.hash] = el;
      tiles.append(el);
    }
    root.append(h("div", { class: "sec", text: label }), tiles);
  }

  await Promise.all(items.map(async (item) => {
    const load = MANAGER_FETCHERS[item.hash];
    if (!load) return;
    try {
      const data = await load(api);
      applyManagerTileData(item.hash, data, tileEls[item.hash], hero);
    } catch (_) {
      // Плитка без цифры, экран не падает (T-19.1-16) — сервер по-прежнему проверял право
      // на каждом из этих же маршрутов, отказ здесь — сетевой/403, не утечка данных.
    }
  }));
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
  // Не-делегат с доступной анкетой (D-24): плитка анкеты над менеджерскими разделами (если
  // они есть) — незарегистрированный менеджер не теряет ни того, ни другого.
  if (!isDelegate && delegateItems.length) {
    await renderTilesOnlyHub(root, ctx, delegateItems);
    if (hasManager) await renderManagerHub(root, ctx, { skipHero: true });
    return;
  }
  if (isDelegate && hasManager) {
    const mode = hubMode();
    root.append(modeSwitch(ctx.h, mode, (next) => {
      setHubMode(next);
      root.replaceChildren();
      renderHub(root, ctx);
    }));
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

  if (!hasSeenOnboarding()) {
    renderOnboarding(root, ctx, () => {
      root.replaceChildren();
      renderHub(root, ctx);
    });
    return;
  }
  await renderHub(root, ctx);
}
