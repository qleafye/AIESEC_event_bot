// Единственный сборщик пары «аватар + имя» на плите (квик 260911-6i9, W3 «Личность и
// приватность»). Листовой модуль БЕЗ импортов — `ui.js` и шесть делегатских экранов сейчас
// зона волны W2 (260911-5ij), их не трогаем; `avatar_url` приходит с сервера уже готовой
// строкой (токен вмонтирован на бэкенде), `fileUrl` из `ui.js` этому модулю не нужен. Ноль
// импортов делает модуль импортируемым в голый node без `app.js` (тот на верхнем уровне читает
// `window.Telegram`/`document.body.dataset` и в последней строке зовёт `start()`, а
// `screens/hub.js` в node вовсе не импортируется) — единственный способ выполнить требование
// «поведение JS — node-тестом» для этого сборщика.
//
// Логика узла аватара — дословный перенос из screens/profile.js (план 23.1-05, UAT D10):
// монограмма-фолбэк подставляется при ошибке загрузки картинки (файл протух у Telegram), а не
// битая иконка; пустое имя рисует `h1` с пустым текстом (тот же паритет, что и раньше у
// профиля). `screens/profile.js` и `screens/hub.js` собирают личность ТОЛЬКО через эту
// функцию — второй копии логики «аватар с фолбэком на монограмму» в проекте не заводится.
export function personNode(h, { avatarUrl, initials, name, sub } = {}, opts = {}) {
  function monoNode() {
    return initials ? h("span", { class: "plate-mono", text: initials }) : null;
  }
  let avatarNode;
  if (avatarUrl) {
    const img = h("img", { class: "plate-avatar", src: avatarUrl, alt: "" });
    img.addEventListener("error", () => img.replaceWith(monoNode() || h("span", {})));
    avatarNode = img;
  } else {
    avatarNode = monoNode();
  }
  const rootClass = opts.compact ? "plate-person plate-person--sm" : "plate-person";
  return h("div", { class: rootClass },
    avatarNode,
    h("div", {},
      h("h1", { text: name || "" }),
      sub ? h("p", { class: "plate-sub", text: sub }) : null,
    ),
  );
}
