// «📊 Статистика геймы» (экран 9 скетча Phase 16 в вебе): плитки чисел и горизонтальные
// полосы по категориям. Полосы — div'ы с шириной в процентах на классах app.css
// (переменные токенов), без Chart.js и без литеральных цветов (D-02). Масштаб — как у
// бота (render_category_bars): максимум = полная полоса, ненулевая категория не короче
// минимума, нулевые пропускаются.

export async function render(root, params, ctx) {
  const { h, api } = ctx;
  const stats = await api("/stats/game");

  root.append(h("h1", { text: "Статистика геймы" }));

  if (!stats.participants) {
    root.append(h("p", { class: "empty", text: "Пока никто ничего не сдавал." }));
    return;
  }

  const tile = (value, label) => h("div", { class: "card stat" },
    h("div", { class: "stat-value", text: String(value) }),
    h("div", { class: "muted", text: label }),
  );
  root.append(
    h("div", { class: "stats" },
      tile(stats.participants, "👥 Участников"),
      tile(stats.submissions.pending, "⏳ На проверке"),
      tile(stats.submissions.approved, "✅ Одобрено"),
      tile(stats.submissions.rejected, "❌ Отклонено"),
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
