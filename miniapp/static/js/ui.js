// Общие построители экранов (план 19.1-05, D-11/D-18): плоская строка списка, надзаголовок
// секции, пустое состояние и состояние ошибки — единая реализация для всех делегатских (и,
// начиная с плана 19.1-06, менеджерских) экранов, чтобы «плоская строка» и «стикер + текст +
// кнопка» не изобретались по-разному в каждом файле.
//
// h передаётся вызывающим экраном (ctx.h) — ui.js не импортирует ядро app.js напрямую, тот же
// приём, что и во внутренних хелперах screens/hub.js (tile(h, navigate, {...})).
//
// Quick 260911-5ij (W2, Пилар 6): тексты состояния «не удалось загрузить» и подпись кнопки
// «Повторить» приходят из `document.body.dataset.screenTexts` — оболочки, а не ответа API.
// Иначе неоткуда взять текст ИМЕННО в момент, когда сам API недоступен (тот же приём, что уже
// есть у экрана «Откройте через бота»/«Заявок»: `page.py::STATE_TEXT_KEYS`/
// `APPLICATIONS_TEXT_KEYS` -> `data-*` на `<body>`). `guardedRender` — единая точка входа
// первичной загрузки всех делегатских экранов: рисует состояние ошибки на любом отказе
// `draw()`, КРОМЕ тех трёх кодов, которые уже покрасило ядро (`api.js:53-62`).

import { icon } from "./icons.js";

// Токен принципала для чтения файлов (quick 260910-w3j): тег <img> физически не может
// послать заголовок X-Telegram-Init-Data, а куки во встроенном браузере Телеграма нет —
// ссылки на файлы собираются ТОЛЬКО здесь, единственный клиентский сборщик
// `/app/api/file/...`. Токен приходит с /app/api/me (app.js вызывает setFileToken после
// загрузки) и живёт FILE_TOKEN_TTL секунд (miniapp/file_tokens.py) — права по нему
// перечитываются сервером на каждый запрос, заморозки прав нет.
let _fileToken = null;

export function setFileToken(token) {
  _fileToken = token || null;
}

export function fileUrl(fileId) {
  const base = `/app/api/file/${encodeURIComponent(fileId)}`;
  return _fileToken ? `${base}?t=${encodeURIComponent(_fileToken)}` : base;
}

// Ведущий эмодзи-кластер подписи (одиночный эмодзи, флаг из двух Regional Indicator, ZWJ-
// последовательность, с необязательным U+FE0F) + пробел(ы) после него.
const LEADING_EMOJI_RE = new RegExp(
  "^(?:(?:\\p{Extended_Pictographic}|\\p{Regional_Indicator})\\uFE0F?"
  + "(?:\\u200D(?:\\p{Extended_Pictographic}|\\p{Regional_Indicator})\\uFE0F?)*\\s*)+",
  "u",
);

// Снимает ведущий эмодзи с подписи реестра перед показом РЯДОМ С Lucide-иконкой строки
// (design rule D-04, план 23.1: одна иконка на строку, не эмодзи и Lucide вместе). Подписи
// разделов (`miniapp_section_*`, settings_schema.py) и вопросов анкеты (REG_LABELS,
// reg_labels.py) по конвенции реестра начинают строку с эмодзи — это верно там, где эмодзи И
// ЕСТЬ иконка (например список настроек «⚙️ Настройки»), но на строках с отдельной Lucide-
// иконкой слева (`flatRow({ icon, title })`) даёт дубль. Сама подпись в реестре НЕ меняется —
// только то, что летит в DOM рядом с иконкой. Обычный текст без эмоджи — без изменений.
export function labelText(label) {
  return String(label || "").replace(LEADING_EMOJI_RE, "");
}

// Склонение счётчиков по-русски (quick 260904-de4, полировка E-настройки): шаблон реестра
// несёт число (`{count}`/`{n}`) и, опционально, группу трёх форм через «|»
// (`{форма1|форма2|форма3}` — 1/немного 2-4/остальное 5+, школьное правило с исключением
// 11-14). Формы приходят ИЗ ШАБЛОНА — литералов множественного числа в этом файле нет
// (сторож `_CYRILLIC` тестов фронта). Шаблон без группы (старое значение, уже сохранённое
// менеджером) — просто подставляет число, ничего не ломает.
const _PLURAL_GROUP_RE = /\{([^{}|]+)\|([^{}|]+)\|([^{}|]+)\}/;

function _ruPluralForm(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function formatCount(template, n) {
  const withNumber = String(template || "").replace(/\{count\}/g, String(n)).replace(/\{n\}/g, String(n));
  const match = _PLURAL_GROUP_RE.exec(withNumber);
  if (!match) return withNumber;
  const form = _ruPluralForm(Math.abs(n), match[1], match[2], match[3]);
  return withNumber.slice(0, match.index) + form + withNumber.slice(match.index + match[0].length);
}

// Плоская строка списка (D-11): иконка/статус-точка/номер места слева, заголовок + мета-строка
// (Label-роль) по центру, trailing (число/чип) справа. Разделитель — hairline на самой строке
// (`.flat-row` в app.css), не рамка вокруг неё; последняя строка внутри `.flat-list` — без
// разделителя (`:last-child` в CSS). `href` — ссылка, `onClick` — кнопка; если не задано ни то,
// ни другое — статичная строка (div), например верхние места рейтинга без перехода.
//
// Phase 23.1: `value` — строка справа от тела (класс `.flat-row-value`, доп. модификатор
// `valueCls`: "ok"/"strong"); `chevron` — булево, добавляет справа шеврон `.flat-row-chev`
// (иконка `chevron-right`). Модификатор `.flush` (app.css, план 23.1-01) превращает
// `.flat-list` в полноширинный список без рамок и радиуса — второго списочного компонента в
// проекте нет, .flush только переопределяет отступы существующего `.flat-list`/`.flat-row`.
// Порядок детей строки: lead, body, value, trailing, chevron.
export function flatRow(h, { icon: iconName, dot, leadText, title, meta, extra, trailing, href, onClick, cls, value, valueCls, chevron }) {
  const lead = iconName
    ? icon(iconName)
    : dot
      ? h("span", { class: `status-dot ${dot}`, "aria-hidden": "true" })
      : leadText != null
        ? h("span", { class: "flat-row-rank", text: leadText })
        : null;
  const body = h("div", { class: "flat-row-body" },
    h("div", { class: "flat-row-title", text: title }),
    meta ? h("div", { class: "flat-row-meta", text: meta }) : null,
    extra || null,
  );
  const valueNode = value != null
    ? h("div", { class: `flat-row-value ${valueCls || ""}`.trim(), text: value })
    : null;
  const tail = trailing != null ? h("div", { class: "flat-row-trailing" }, trailing) : null;
  const chev = chevron ? h("span", { class: "flat-row-chev" }, icon("chevron-right")) : null;
  const attrs = { class: `flat-row ${cls || ""}`.trim() };
  if (href) {
    attrs.href = href;
    if (onClick) attrs.onClick = onClick;
    return h("a", attrs, lead, body, valueNode, tail, chev);
  }
  if (onClick) {
    attrs.type = "button";
    attrs.onClick = onClick;
    return h("button", attrs, lead, body, valueNode, tail, chev);
  }
  return h("div", attrs, lead, body, valueNode, tail, chev);
}

// Надзаголовок секции (12px, Lato 700, letter-spacing .08em, верхний регистр, --text-faint) —
// та же типографика, что уже была введена в hub.js как класс `.sec` (план 19.1-04); правило в
// app.css общее для обоих классов, чтобы они не разошлись.
export function sectionTitle(h, text) {
  return h("div", { class: "section-title", text });
}

// Плитка хаба (`.tiles`/`.tile` app.css, план 19.1-04) — иконка + подпись + мета-строка,
// вынесена из hub.js (Phase 22 Plan 07, D-16) для повторного использования на стартовом
// экране настроек (плитки разделов). `dot` — несохранённые правки раздела (маркер `.tile-
// dot`, тот же приём, что status-dot у flatRow), без него разметка не меняется.
export function tile(h, { onClick, iconName, label, meta, dot }) {
  return h("button", {
    type: "button",
    class: `tile${dot ? " has-dot" : ""}`,
    onClick,
  },
    iconName ? icon(iconName) : null,
    h("b", { text: label }),
    h("small", { text: meta }),
    dot ? h("span", { class: "tile-dot", "aria-hidden": "true" }) : null,
  );
}

// Слоты стикеров пресета (D-04/D-18) — file_id приходит из ctx.me (/app/api/me, план 19.1-02).
const STICKER_FIELD = {
  empty: "sticker_empty_file_id",
  success: "sticker_success_file_id",
  error: "sticker_error_file_id",
  top1: "sticker_top1_file_id",
};

function stateShell(h, { me, slot, text, action, cls }) {
  const fileId = me ? me[STICKER_FIELD[slot]] : null;
  const wrap = h("div", { class: `empty-state ${cls || ""}`.trim() });
  if (fileId) {
    const img = h("img", { class: "sticker", alt: "", src: fileUrl(fileId) });
    // Стикер не задан менеджером или не грузится — состояние всё равно корректно: текст и
    // кнопка остаются, дыры на месте картинки не возникает (D-18).
    img.addEventListener("error", () => img.remove());
    wrap.append(img);
  }
  wrap.append(h("p", { class: "empty-state-text", text: text || "" }));
  if (action) wrap.append(action);
  return wrap;
}

// Пустое состояние (D-18): стикер-слот пресета + строка ИЗ ПЕРЕДАННЫХ ДАННЫХ (реестр, 0
// хардкода — текст всегда приходит с сервера полем `empty_text`, не литералом) + одна
// кнопка-действие.
export function emptyState(h, { me, slot = "empty", text, action } = {}) {
  return stateShell(h, { me, slot, text, action });
}

// То же самое на слоте «ошибка» + кнопка повтора вместо произвольного action.
export function errorState(h, { me, text, retry } = {}) {
  const action = retry
    ? h("button", { class: "btn secondary", type: "button", text: screenText("retry"), onClick: retry })
    : null;
  return stateShell(h, { me, slot: "error", text, action, cls: "error-state" });
}

// Разбор data-screen-texts мемоизирован — атрибут неизменен на всё время жизни страницы
// (задаётся один раз сервером при рендере оболочки), повторный JSON.parse на каждый вызов
// не нужен. Битый JSON/отсутствие атрибута — тихий пустой объект, а не исключение (fail-soft,
// тот же принцип, что applications.js::applicationsTexts).
let _screenTextsCache = null;

export function screenText(name) {
  if (_screenTextsCache === null) {
    try {
      _screenTextsCache = JSON.parse((document.body && document.body.dataset.screenTexts) || "{}");
    } catch (_) {
      _screenTextsCache = {};
    }
  }
  return _screenTextsCache[name] || "";
}

// Экран уже покрасило ядро (app.js -> api.js::authErrorHandler): 401, 403 и 503 РОВНО с
// reason "miniapp_off" — источник истины `api.js:53-62`. Копии этой проверки в отдельных
// экранах (`faq.js`/`form.js`) сверяли только `status === 503` целиком — для 503 с другим
// reason (сам аппсервер лежит) это ложное «ядро покрасило», хотя оно не красило. Общая
// проверка обязана смотреть и на reason.
export function isCoreHandledError(err) {
  if (!err) return false;
  if (err.status === 401 || err.status === 403) return true;
  return err.status === 503 && err.reason === "miniapp_off";
}

// Единая точка входа первичной загрузки делегатского экрана (Пилар 6, BLOCKER Топ-10 п.1):
// без неё отказ сервера/обрыв сети на первом api() оставляет `root` пустым или навсегда
// «Загрузка…» — роутер ядра на ApiError делает `return;` (app.js:524), сам экран себя не красит.
// `draw` — прежнее тело render() экрана; `guardedRender` чистит `root` перед каждой попыткой
// (повтор не дописывает второй экран под первым) и не трогает `root`, если экран уже покрасило
// ядро (см. isCoreHandledError) — иначе перекрасили бы «Нет доступа»/«Сессия истекла» текстом
// «не удалось загрузить».
export async function guardedRender(root, ctx, draw) {
  root.replaceChildren();
  try {
    await draw();
  } catch (err) {
    if (isCoreHandledError(err)) return;
    root.replaceChildren(errorState(ctx.h, {
      me: ctx.me,
      text: screenText("load_error"),
      // Возвращает промис (а не «дозвонился и забыл») — guardedRender сам никогда не
      // отклоняется (ловит всё внутри), поэтому неперехваченного отказа тут не будет; форма
      // как у "Показать ещё" в tasks.js (onClick: load) — вызывающий МОЖЕТ await, тестам это
      // нужно для детерминизма клика.
      retry: () => guardedRender(root, ctx, draw),
    }));
  }
}
