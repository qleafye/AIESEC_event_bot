// Задания менеджера (экран 6 скетча Phase 16 в вебе): тумблер «Активные | Архив» — два
// состояния, список перерисовывается без перезагрузки; строка «№N · название · категория ·
// монеты · до дд.мм» со счётчиками «на проверке / принято», «Показать ещё», «➕ Новое
// задание» -> #/task-edit/new, тап по строке -> #/task-edit/{id}.
// Подписи категорий приходят с сервера готовыми (RU) — фронт кодов не знает.

const PAGE = 25;

function taskRow(h, navigate, item) {
  const open = () => navigate(`#/task-edit/${item.id}`);
  const meta = [item.category_label, `${item.coins}🪙`, `до ${item.deadline_short}`].join(" · ");
  const counters = [];
  if (item.pending > 0) counters.push(h("span", { class: "chip warn", text: `на проверке: ${item.pending}` }));
  if (item.approved > 0) counters.push(h("span", { class: "chip success", text: `принято: ${item.approved}` }));
  if (item.overdue && !item.archived) counters.push(h("span", { class: "chip", text: "срок вышел" }));
  if (item.has_photo) counters.push(h("span", { class: "faint", text: "📷" }));
  return h("article", {
    class: `card task clickable admin-task-row${item.archived ? " archived" : ""}`,
    role: "link",
    tabindex: "0",
    onClick: open,
    onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") open(); },
  },
    h("div", { class: "task-head" },
      h("span", { class: "task-number", text: `№${item.number}` }),
      h("div", { class: "title", text: item.title }),
    ),
    h("div", { class: "muted meta", text: meta }),
    counters.length ? h("div", { class: "task-foot" }, counters) : null,
  );
}

export async function render(root, params, ctx) {
  const { h, api, navigate } = ctx;

  let archived = false;
  let offset = 0;
  let total = 0;
  let loading = false;

  const activeBtn = h("button", { class: "btn toggle-btn active", type: "button", text: "Активные" });
  const archiveBtn = h("button", { class: "btn toggle-btn", type: "button", text: "Архив" });
  const toggle = h("div", { class: "toggle", role: "tablist" }, activeBtn, archiveBtn);
  const list = h("div", { class: "task-grid" });
  const foot = h("div", { class: "list-foot" });
  const newBtn = h("button", {
    class: "btn", type: "button", text: "➕ Новое задание",
    onClick: () => navigate("#/task-edit/new"),
  });

  root.append(h("h1", { text: "Задания" }), toggle, newBtn, list, foot);

  function paintToggle(page) {
    activeBtn.textContent = `Активные (${page.active_count})`;
    archiveBtn.textContent = `Архив (${page.archived_count})`;
    activeBtn.classList.toggle("active", !archived);
    archiveBtn.classList.toggle("active", archived);
    activeBtn.setAttribute("aria-selected", String(!archived));
    archiveBtn.setAttribute("aria-selected", String(archived));
  }

  async function load() {
    if (loading) return;
    loading = true;
    foot.replaceChildren(h("div", { class: "loading", text: "Загрузка…" }));
    let page;
    try {
      page = await api(`/admin/tasks?archived=${archived ? 1 : 0}&offset=${offset}&limit=${PAGE}`);
    } catch (err) {
      loading = false;
      if (!(err && (err.status === 401 || err.status === 403 || err.status === 503))) {
        foot.replaceChildren(h("p", { class: "error-inline", text: "Не удалось загрузить задания — попробуйте ещё раз." }));
      }
      return;
    }
    loading = false;
    total = page.total;
    paintToggle(page);
    for (const item of page.items) list.append(taskRow(h, navigate, item));
    offset += page.items.length;
    foot.replaceChildren();
    if (total === 0) {
      foot.append(h("div", { class: "empty" }, h("p", { text: page.empty_text || "" })));
      return;
    }
    if (offset < total) {
      foot.append(h("button", {
        class: "btn secondary", type: "button",
        text: `Показать ещё (${total - offset})`,
        onClick: load,
      }));
    } else {
      foot.append(h("p", { class: "faint center", text: archived ? `В архиве: ${total}` : `Активных заданий: ${total}` }));
    }
  }

  // Переключение без перезагрузки: сброс страницы и новый запрос с другим флагом.
  function switchTo(toArchive) {
    if (archived === toArchive || loading) return;
    archived = toArchive;
    offset = 0;
    total = 0;
    list.replaceChildren();
    load();
  }
  activeBtn.addEventListener("click", () => switchTo(false));
  archiveBtn.addEventListener("click", () => switchTo(true));

  await load();
}
