// Экран #/program — «📅 Программа» делегата (D-29, владелец 24.09): одна кнопка программы, вид
// выбирает менеджер (`program_miniapp_view`, per_city) — «таблица» из программы бота или фото.
// Что показывать, решает сервер (`GET /app/api/program`, services/program.py — та же точка
// правды, что у кнопки в чате); клиент только рисует готовый ответ:
//   - таблица: дни -> слоты (параллельные сессии уже сгруппированы сервером) -> сессии; флаги
//     `now`/`next` приходят с сервера (МСК), здесь не пересчитываются;
//   - фото: картинка на ширину экрана, тап открывает её крупно поверх экрана;
//   - пусто: `empty_text` из реестра (тот же ключ, что у бота), никогда не пустой экран.
// Подписи («Идёт сейчас», «Зал:», …) приходят полем `texts` уже переведёнными — 0 литералов.

import { emptyState, errorState, errorText, labelText, guardedRender, screenText } from "../ui.js";
import { icon } from "../icons.js";
import { stagger } from "../motion.js";

function sectionLabel(section) {
  try {
    const labels = JSON.parse(document.body.dataset.sectionLabels || "{}");
    return labels[section] || "";
  } catch (_) {
    return "";
  }
}

// «пятница, 30 октября» — день недели помогает на двухдневном форуме больше, чем «30.10.2026».
// Любой сбой разбора — подпись сервера (`day.label`) как есть.
function dayHeading(day, lang) {
  try {
    const date = new Date(`${day.day}T00:00:00`);
    if (Number.isNaN(date.getTime())) return day.label;
    const text = new Intl.DateTimeFormat(lang === "en" ? "en-GB" : "ru-RU", {
      weekday: "long", day: "numeric", month: "long",
    }).format(date);
    return text.charAt(0).toUpperCase() + text.slice(1);
  } catch (_) {
    return day.label;
  }
}

function timeRange(start, end) {
  return end ? `${start}–${end}` : start;
}

function markerChip(h, flags, texts) {
  if (flags.now) return h("span", { class: "chip danger program-marker", text: texts.now });
  if (flags.next) return h("span", { class: "chip accent program-marker", text: texts.next });
  return null;
}

function sessionCard(h, session, texts, { withTime }) {
  const meta = [];
  if (session.hall_name) meta.push(`${texts.hall} ${session.hall_name}`);
  if (session.speaker) meta.push(`${texts.speaker} ${session.speaker}`);
  // Параллельная сессия несёт свою метку «идёт/следующая» (флаги сессии с сервера): слот
  // 03:00–06:00 уже идёт, а его сессия с 04:30 — ещё нет.
  return h("div", { class: "program-session" },
    withTime ? h("div", { class: "program-session-time" },
      h("span", { text: timeRange(session.start_time, session.end_time) }),
      markerChip(h, session, texts),
    ) : null,
    h("div", { class: "program-session-title", text: session.title }),
    meta.length ? h("div", { class: "program-session-meta", text: meta.join(" · ") }) : null,
  );
}

function slotRow(h, slot, texts) {
  const parallel = slot.sessions.length > 1;
  // У параллельного слота метка — на каждой сессии (sessionCard), не на слоте целиком.
  const marker = parallel ? null : markerChip(h, slot, texts);
  const head = h("div", { class: "program-slot-head" },
    h("span", { class: "program-slot-time", text: timeRange(slot.start_time, slot.end_time) }),
    parallel ? h("span", { class: "program-slot-parallel", text: texts.parallel }) : null,
    marker,
  );
  // Параллельные сессии — сеткой рядом (на узком экране по одной в строку), у каждой своё
  // время: внутри слота они могут начинаться не одновременно (группировка транзитивная).
  const body = h("div", { class: parallel ? "program-parallel" : "" },
    slot.sessions.map((s) => sessionCard(h, s, texts, { withTime: parallel })),
  );
  let cls = "program-slot";
  if (slot.now) cls += " is-now";
  else if (slot.next) cls += " is-next";
  return h("div", { class: cls }, head, body);
}

function renderTable(h, root, page) {
  const texts = page.texts || {};
  const days = page.days || [];
  let focus = null;

  if (days.length > 1) {
    const chips = h("div", { class: "program-days" });
    days.forEach((day, index) => {
      chips.append(h("button", {
        class: "chip program-day-chip", type: "button", text: dayHeading(day, page.lang),
        onClick: () => {
          const target = root.querySelector(`[data-day-index="${index}"]`);
          if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
        },
      }));
    });
    root.append(chips);
  }

  days.forEach((day, index) => {
    const list = h("div", { class: "program-day-list" });
    for (const slot of day.slots || []) {
      const row = slotRow(h, slot, texts);
      if (!focus && (slot.now || slot.next)) focus = row;
      list.append(row);
    }
    root.append(
      h("section", { class: "program-day", "data-day-index": String(index) },
        h("h2", { class: "program-day-title", text: dayHeading(day, page.lang) }),
        list,
      ),
    );
    stagger(list);
  });

  // Сразу к текущей/следующей сессии — во время форума делегат открывает экран ради неё.
  if (focus) {
    requestAnimationFrame(() => focus.scrollIntoView({ block: "center" }));
  }
}

function openZoom(h, src, alt) {
  // Оверлей живёт на body, вне экрана: уход с маршрута (BackButton Telegram) закрывает и его.
  const close = () => {
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("hashchange", close);
    overlay.remove();
  };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const overlay = h("div", { class: "program-zoom", role: "dialog", "aria-modal": "true", onClick: close },
    h("button", { class: "program-zoom-close", type: "button", "aria-label": alt, onClick: close }, icon("x")),
    // Прокрутка внутри оверлея — вместо щипка: вебвью Telegram держит масштаб страницы
    // запертым, крупная картинка листается пальцем по обеим осям.
    h("div", { class: "program-zoom-scroll", onClick: (event) => event.stopPropagation() },
      h("img", { class: "program-zoom-img", src, alt }),
    ),
  );
  document.addEventListener("keydown", onKey);
  window.addEventListener("hashchange", close);
  document.body.append(overlay);
}

function renderPhoto(h, root, page, title) {
  const img = h("img", {
    class: "program-photo", src: page.photo_url, alt: title, loading: "lazy", decoding: "async",
  });
  root.append(h("button", {
    class: "program-photo-button", type: "button", "aria-label": title,
    onClick: () => openZoom(h, page.photo_url, title),
  }, img));
}

async function draw(root, params, ctx) {
  const { h, api, me } = ctx;
  const title = labelText(sectionLabel("program"));
  root.append(h("h1", { text: title }));

  let page;
  try {
    page = await api("/program");
  } catch (err) {
    // Сервер не смог прочитать город делегата (503 retry) — честная ошибка с повтором и его
    // текстом, а не чужое расписание (miniapp/routers/program.py::_retry_error).
    if (err && err.status === 503 && err.reason === "retry") {
      root.append(errorState(h, {
        me, text: errorText(err, screenText("load_error")),
        retry: () => render(root, params, ctx),
      }));
      return;
    }
    throw err;
  }

  if (page.view === "photo" && page.photo_url) {
    renderPhoto(h, root, page, title);
    return;
  }
  if (page.view === "table" && (page.days || []).length) {
    renderTable(h, root, page);
    return;
  }
  root.append(emptyState(h, { me, text: page.empty_text || "" }));
}

export async function render(root, params, ctx) {
  await guardedRender(root, ctx, () => draw(root, params, ctx));
}
