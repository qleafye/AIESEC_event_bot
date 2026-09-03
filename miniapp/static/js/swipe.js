// swipe.js — распознавание жеста «принять/отклонить» (D-04, T-23-22/T-23-23/T-23-24).
// Telegram WebApp даёт управление ТОЛЬКО вертикальным свайпом
// (disableVerticalSwipes/enableVerticalSwipes/isVerticalSwipesEnabled, Bot API 7.7+) — метода
// «выключить горизонтальный свайп» или «свайп назад» в API нет вовсе, системный «назад» живёт
// у левого края экрана. Поэтому «это жест решения по карточке, а не прокрутка и не системный
// „назад“» решается ЗДЕСЬ, чистой функцией, без единого обращения к DOM: преобладание
// горизонтали над вертикалью, порог сдвига и мёртвая зона у левого края.
//
// `swipeDecision` — чистая (проверяется импортом модуля в голом node, без document/window).
// `attachSwipe` — тонкая DOM-обвязка поверх неё на pointer-событиях.

export const HORIZONTAL_MIN = 10; // px — короче этого дрожание пальца не двигает карточку вовсе
export const COMMIT_PX = 96; // px — абсолютный порог решения (узкие экраны)
export const COMMIT_RATIO = 0.32; // доля ширины карточки — порог решения (широкие экраны)
export const MAX_TILT = 10; // градусы — предел наклона карточки при полном сдвиге
export const EDGE_GUARD = 24; // px от левого края — там живёт системный жест «назад»
// tan(30°) — тот же порог, что «угол круче ~30° от горизонтали»: |dy| > |dx| * VERTICAL_SLOPE_MAX
// покрывает и явный случай |dy| > |dx| (там |dy|/|dx| > 1 > VERTICAL_SLOPE_MAX), и более пологий
// «уже не горизонталь» случай — одним неравенством вместо двух отдельных проверок.
export const VERTICAL_SLOPE_MAX = 0.5774;

/**
 * Решение по смещению пальца от начала жеста: `approve`/`reject`/ничего. Чистая функция — ни
 * одного обращения к document/window/Telegram, поведение целиком по (dx, dy, width, startX).
 * @param {{dx: number, dy: number, width: number, startX?: number}} gesture
 * @returns {{action: "approve"|"reject"|null, vertical?: boolean, edge?: boolean, progress?: number, tilt?: number}}
 */
export function swipeDecision({ dx, dy, width, startX } = {}) {
  const ddx = dx || 0;
  const ddy = dy || 0;
  const adx = Math.abs(ddx);
  const ady = Math.abs(ddy);
  const w = width > 0 ? width : 0;

  // T-23-24: жест, начатый у левого края, отдаём системному «назад» безоговорочно — раньше
  // любой другой проверки, иначе широкий свайп вправо от края посчитался бы решением.
  if (startX != null && startX < EDGE_GUARD) return { action: null, edge: true };

  // Преобладание вертикали — карточка не двигается вовсе, жест идёт прокрутке (T-23-22).
  if (ady > adx * VERTICAL_SLOPE_MAX) return { action: null, vertical: true };

  // Короче порога дрожания — тап/дрожание пальца, не жест решения.
  if (adx < HORIZONTAL_MIN) return { action: null };

  const commitAt = Math.max(COMMIT_PX, w * COMMIT_RATIO);
  const progress = Math.min(1, adx / commitAt);
  const tilt = (ddx < 0 ? -1 : 1) * progress * MAX_TILT;

  // T-23-22: решение только за порогом commitAt — случайный свайп при прокрутке длинной
  // карточки его не пересекает.
  if (adx >= commitAt) return { action: ddx > 0 ? "approve" : "reject", progress: 1, tilt };

  return { action: null, progress, tilt };
}

/**
 * DOM-обвязка `swipeDecision` на pointer-событиях: pointerdown ловит старт, pointermove кормит
 * `onProgress` (двигать карточку/оверлей), pointerup решает commit/cancel, pointercancel всегда
 * отменяет. `touch-action: pan-y` ставится на элемент отсюда — браузер сам отдаёт вертикаль
 * прокрутке, наш код разбирает только горизонталь. Уровень анимации "off" (motion.js) отключает
 * ПРОМЕЖУТОЧНЫЙ `onProgress`-трансформ (нечего анимировать без движения), но не отключает сам
 * жест — commit/cancel по-прежнему считаются и вызываются.
 *
 * Владелец (03.09, стенд с телефона, «дёргано»): pointermove на сенсоре может прилетать чаще
 * частоты кадров — раньше каждое сырое событие СРАЗУ писало `el.style.transform` (запись стиля
 * вне кадра отрисовки — источник дёрганости, конкурирует с браузерным layout/paint). Теперь
 * `handleMove` только запоминает последнее событие; сам вызов `onProgress` идёт максимум раз за
 * кадр через `requestAnimationFrame` (coalescing) — несколько pointermove между кадрами схлопы-
 * ваются в одно обновление transform. Класс `DRAG_CLASS` на элементе снимает CSS-transition на
 * время жеста (app.css `.is-dragging`) — иначе transform из onProgress «боролся» бы с переходом
 * к предыдущему значению на каждый кадр; при pointerup/pointercancel класс снимается, и сброс
 * `el.style.transform` (см. screens/applications.js::resetCardTransform) уже анимируется этим же
 * CSS-transition (200–250мс, cubic-bezier) вместо мгновенного скачка.
 * @param {HTMLElement} el
 * @param {{onProgress?: (decision: object, raw: {dx:number, dy:number}) => void,
 *          onCommit?: (action: "approve"|"reject") => void,
 *          onCancel?: (decision: object) => void}} handlers
 * @returns {() => void} detach — снимает все слушатели (звать из unmount())
 */
const DRAG_CLASS = "is-dragging";
// Владелец (03.09, стенд с телефона): кнопка «Показать всё» внутри карточки переставала
// открываться после того, как attachSwipe стал ловить pointerdown на всей карточке и звать
// `el.setPointerCapture` — начиная с этого кадра ВСЕ последующие pointer-события (и производный
// click) для этого pointerId браузер ретаргетит на захвативший элемент (саму карточку), а не на
// исходную кнопку/ссылку под пальцем (тот же механизм ломал бы и открытие резюме-ссылки, и
// раскрытие истории `<summary>`). Жест решения — только по самой карточке; тап по интерактивному
// потомку (кнопка/ссылка/summary/поле ввода) не должен становиться стартом захвата вовсе.
const INTERACTIVE_SELECTOR = "button, a, input, textarea, select, summary, label, [data-no-swipe]";

function isInteractiveTarget(target) {
  return !!(target && typeof target.closest === "function" && target.closest(INTERACTIVE_SELECTOR));
}

export function attachSwipe(el, { onProgress, onCommit, onCancel } = {}) {
  if (!el) return () => {};
  el.style.touchAction = "pan-y";

  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let rafId = null;
  let pendingEvent = null; // последнее необработанное pointermove — коалесинг до кадра

  function motionOff() {
    return document.documentElement.dataset.motion === "off";
  }

  function cancelPendingFrame() {
    if (rafId != null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    pendingEvent = null;
  }

  function reset() {
    pointerId = null;
    cancelPendingFrame();
    el.classList.remove(DRAG_CLASS);
  }

  function gestureFrom(e) {
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    const width = el.clientWidth || 0;
    return swipeDecision({ dx, dy, width, startX });
  }

  // Один onProgress за кадр отрисовки, а не за pointer-событие (сенсор может слать move чаще
  // частоты кадров) — раннее событие того же кадра просто перезаписывается более свежим.
  function flushPendingFrame() {
    rafId = null;
    const e = pendingEvent;
    pendingEvent = null;
    if (!e || pointerId == null || e.pointerId !== pointerId || motionOff() || !onProgress) return;
    const decision = gestureFrom(e);
    onProgress(decision, { dx: e.clientX - startX, dy: e.clientY - startY });
  }

  function handleDown(e) {
    if (pointerId != null) return; // уже ведём один жест — второй палец игнорируем
    if (isInteractiveTarget(e.target)) return; // тап по кнопке/ссылке карточки — не жест решения
    pointerId = e.pointerId;
    startX = e.clientX;
    startY = e.clientY;
    el.classList.add(DRAG_CLASS); // снимает CSS-transition на время жеста (app.css)
    if (typeof el.setPointerCapture === "function") {
      try {
        el.setPointerCapture(pointerId);
      } catch (_) {
        // клиент без поддержки capture для этого типа указателя — жест всё равно работает
      }
    }
  }

  function handleMove(e) {
    if (pointerId == null || e.pointerId !== pointerId) return;
    pendingEvent = e;
    if (rafId == null) rafId = requestAnimationFrame(flushPendingFrame);
  }

  function handleUp(e) {
    if (pointerId == null || e.pointerId !== pointerId) return;
    const decision = gestureFrom(e);
    reset();
    if (decision.action && onCommit) onCommit(decision.action);
    else if (onCancel) onCancel(decision);
  }

  function handleCancel(e) {
    if (pointerId == null || e.pointerId !== pointerId) return;
    reset();
    if (onCancel) onCancel({ action: null, cancelled: true });
  }

  // passive: true — ни один обработчик здесь не зовёт preventDefault (вертикаль браузер уже
  // отдаёт прокрутке через touch-action: pan-y), явная пометка снимает с браузера необходимость
  // ждать возможный preventDefault перед скроллом/отрисовкой кадра.
  const listenerOpts = { passive: true };
  el.addEventListener("pointerdown", handleDown, listenerOpts);
  el.addEventListener("pointermove", handleMove, listenerOpts);
  el.addEventListener("pointerup", handleUp, listenerOpts);
  el.addEventListener("pointercancel", handleCancel, listenerOpts);

  return function detach() {
    cancelPendingFrame();
    el.removeEventListener("pointerdown", handleDown, listenerOpts);
    el.removeEventListener("pointermove", handleMove, listenerOpts);
    el.removeEventListener("pointerup", handleUp, listenerOpts);
    el.removeEventListener("pointercancel", handleCancel, listenerOpts);
  };
}
