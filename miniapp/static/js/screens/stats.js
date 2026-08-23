// «Статистика геймы» (экран 9 скетча Phase 16 в вебе, editorial-минимал 19.1-06): плитки
// чисел (Display-роль, tabular-nums, иконка-метка) и горизонтальные полосы по категориям.
// Полосы — div'ы с шириной в процентах на классах app.css (переменные токенов), без Chart.js
// и без литеральных цветов. Масштаб — как у бота (render_category_bars): максимум = полная
// полоса, ненулевая категория не короче минимума, нулевые пропускаются. Порядок категорий —
// как отдаёт API (`by_category`), фиксированный набор категорий геймы, не сортировка «как
// пришло». Заливка полосы — цвет серии данных `--chart-1`, трек — `--chart-track`, не акцент
// интерфейса (dataviz-контракт, UI-SPEC §Dataviz, находка №10 refs/design-review/dataviz.md).

import { icon } from "../icons.js";
import { emptyState } from "../ui.js";

export async function render(root, params, ctx) {
  const { h, api, me } = ctx;
  const stats = await api("/stats/game");

  root.append(h("h1", { text: "Статистика геймы" }));

  if (!stats.participants) {
    root.append(emptyState(h, { me, text: "Пока никто ничего не сдавал." }));
    return;
  }

  const tile = (iconName, value, label) => h("div", { class: "card stat" },
    icon(iconName),
    h("div", { class: "stat-value", text: String(value) }),
    h("div", { class: "label-role", text: label }),
  );
  root.append(
    h("div", { class: "stats" },
      tile("user", stats.participants, "Участников"),
      tile("clock", stats.submissions.pending, "На проверке"),
      tile("check", stats.submissions.approved, "Одобрено"),
      tile("x", stats.submissions.rejected, "Отклонено"),
    ),
  );

  const rows = stats.by_category.filter((r) => r.count > 0);
  const block = h("div", { class: "card" }, h("h2", { text: "По категориям (одобрено)" }));
  if (!rows.length) {
    block.append(h("p", { class: "muted", text: "пока нет одобренных сдач" }));
  } else {
    const max = Math.max(...rows.map((r) => r.count));
    for (const r of rows) {
      const pct = Math.max(10, Math.round((100 * r.count) / max));
      const fill = h("div", { class: "bar-fill" });
      fill.style.width = `${pct}%`;
      block.append(
        h("div", { class: "bar-row" },
          h("span", { class: "bar-label", text: r.label }),
          h("div", { class: "bar-track" }, fill),
          h("span", { class: "bar-count", text: String(r.count) }),
        ),
      );
    }
  }
  root.append(block);
}

export function unmount() {
  // Экран без MainButton и без состояния.
}
