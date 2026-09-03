// Общие построители экранов (план 19.1-05, D-11/D-18): плоская строка списка, надзаголовок
// секции, пустое состояние и состояние ошибки — единая реализация для всех делегатских (и,
// начиная с плана 19.1-06, менеджерских) экранов, чтобы «плоская строка» и «стикер + текст +
// кнопка» не изобретались по-разному в каждом файле.
//
// h передаётся вызывающим экраном (ctx.h) — ui.js не импортирует ядро app.js напрямую, тот же
// приём, что и во внутренних хелперах screens/hub.js (tile(h, navigate, {...})).

import { icon } from "./icons.js";

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
    const img = h("img", { class: "sticker", alt: "", src: `/app/api/file/${encodeURIComponent(fileId)}` });
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
    ? h("button", { class: "btn secondary", type: "button", text: "Повторить", onClick: retry })
    : null;
  return stateShell(h, { me, slot: "error", text, action, cls: "error-state" });
}
