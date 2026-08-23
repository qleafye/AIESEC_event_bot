// Хаб делегата и менеджера (план 19.1-04, D-09 — делегат заперт на вариант C; D-10 — тот же
// компонент по умолчанию для менеджера, пока голосование команды не выбрало A/B/C) + одно-
// разовый привет-экран. Дом приложения при NAV_LAYOUT === "hub" (см. app.js).
//
// Состав плиток строится ТЕМ ЖЕ visibleNav(), что и раскладки таб-бара/верхних табов —
// выключенный чекбоксом раздел не даёт плитку, права по-прежнему проверяет сервер на каждом
// маршруте (T-19.1-14). Счётчики менеджера — существующие списочные ручки /app/api/* с
// минимальным limit, каждый запрос fail-soft по отдельности (T-19.1-16): не ответившая ручка
// даёт плитку без цифры, а не пустой экран.

import { visibleNav, NAV_ICONS } from "../app.js";
import { icon } from "../icons.js";
import { countUp } from "../motion.js";

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

function shortDeadline(item) {
  if (item.status === "approved") {
    return item.coins_awarded != null ? `+${item.coins_awarded}🪙` : `${item.coins}🪙`;
  }
  return `${item.coins}🪙`;
}

function deadlineRow(h, item) {
  const statusMeta = item.status === "approved"
    ? "принято"
    : (item.overdue ? "срок вышел" : `до ${item.deadline_short}`);
  return h("div", { class: "row" },
    h("div", {},
      h("div", { text: item.title }),
      h("div", { class: "faint", text: [item.category_label, statusMeta].filter(Boolean).join(" · ") }),
    ),
    h("span", { class: "stat-value", text: shortDeadline(item) }),
  );
}

function daysSince(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  if (!m) return null;
  const submittedAt = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const days = Math.max(0, Math.floor((Date.now() - submittedAt) / 86400000));
  return days;
}

// ── привет-экран (D-09): показывается один раз, оба текста из реестра ───────────────────
function renderOnboarding(root, ctx, onDone) {
  const { h, me } = ctx;
  const dark = document.documentElement.dataset.theme === "dark";
  const coverId = (dark && me.cover_dark_file_id) ? me.cover_dark_file_id : me.cover_file_id;
  const wrap = h("section", { class: "onboarding" });
  if (coverId) {
    const img = h("img", { class: "onboarding-cover", src: `/app/api/file/${coverId}`, alt: "" });
    img.addEventListener("error", () => img.remove()); // обложки нет/не грузится — экран всё равно корректен
    wrap.append(img);
  }
  wrap.append(
    h("p", { class: "onboarding-text", text: me.onboarding_text || "" }),
    h("button", {
      class: "btn", type: "button", text: me.onboarding_cta || "",
      onClick: () => { markOnboardingSeen(); onDone(); },
    }),
  );
  root.append(wrap);
}

// ── хаб делегата (D-09, вариант C — заперт, не часть голосования) ───────────────────────
async function renderDelegateHub(root, ctx) {
  const { h, api, navigate } = ctx;
  const labels = sectionLabelsFromDom();
  const items = visibleNav().filter((item) => item.delegate);

  const heroBig = h("div", { class: "big", text: "0" });
  const heroLbl = h("div", { class: "lbl", text: "монет" });
  const hero = h("section", { class: "hero" },
    h("span", { class: "hero-pattern", "aria-hidden": "true" }),
    heroBig, heroLbl,
  );
  root.append(hero);

  const tiles = h("div", { class: "tiles" });
  const tileEls = {};
  for (const item of items) {
    const el = tile(h, navigate, {
      hash: item.hash,
      iconName: NAV_ICONS[item.hash],
      label: labels[item.section] || item.section,
      meta: "…",
    });
    tileEls[item.hash] = el;
    tiles.append(el);
  }
  root.append(tiles);

  const deadlineSec = h("div", { class: "sec", text: "Ближайший дедлайн" });
  const deadlineList = h("div", { class: "card list-card" });
  root.append(deadlineSec, deadlineList);

  const [balanceR, historyR, profileR, tasksR] = await Promise.allSettled([
    api("/coins/balance"),
    api("/coins/history?offset=0&limit=1"),
    api("/profile"),
    api("/tasks?offset=0&limit=2"),
  ]);

  if (balanceR.status === "fulfilled") {
    const bal = balanceR.value;
    countUp(heroBig, 0, bal.balance || 0);
    if (bal.rank != null) {
      hero.append(h("span", {
        class: "rank",
        text: bal.participants ? `${bal.rank}-й из ${bal.participants}` : `${bal.rank}-й`,
      }));
    }
    if (tileEls["#/leaderboard"]) {
      tileEls["#/leaderboard"].querySelector("small").textContent =
        bal.rank == null ? "пока без места" : `ты ${bal.rank}-й`;
    }
  }
  if (historyR.status === "fulfilled" && tileEls["#/coins"]) {
    tileEls["#/coins"].querySelector("small").textContent = `${historyR.value.total} операций`;
  }
  if (profileR.status === "fulfilled" && tileEls["#/profile"]) {
    tileEls["#/profile"].querySelector("small").textContent = profileR.value.payment_status_label || "профиль";
  }
  if (tasksR.status === "fulfilled") {
    const page = tasksR.value;
    if (tileEls["#/tasks"]) {
      tileEls["#/tasks"].querySelector("small").textContent = `${page.total} активных`;
    }
    if (page.items.length) {
      for (const item of page.items) deadlineList.append(deadlineRow(h, item));
    } else {
      deadlineList.append(h("div", { class: "empty", text: page.empty_text || "" }));
    }
  } else {
    deadlineSec.remove();
    deadlineList.remove();
  }
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

async function renderManagerHub(root, ctx) {
  const { h, api, navigate } = ctx;
  const labels = sectionLabelsFromDom();
  const items = visibleNav().filter((item) => !item.delegate);

  const hero = h("section", { class: "hero hero-flat" },
    h("div", { class: "big", text: "0" }),
    h("div", { class: "lbl", text: "сдач на проверке" }),
  );
  root.append(hero);

  const tiles = h("div", { class: "tiles" });
  const tileEls = {};
  for (const item of items) {
    const el = tile(h, navigate, {
      hash: item.hash,
      iconName: NAV_ICONS[item.hash],
      label: labels[item.section] || item.section,
      meta: "…",
    });
    tileEls[item.hash] = el;
    tiles.append(el);
  }
  root.append(tiles);

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

async function renderHub(root, ctx) {
  if (ctx.me.is_delegate) await renderDelegateHub(root, ctx);
  else await renderManagerHub(root, ctx);
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
