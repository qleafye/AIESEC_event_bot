// Motion (D-17): единственное место, где живёт знание об уровнях движения. Экраны и ядро
// зовут готовые помощники (confetti/countUp/haptic/slideIn/stagger/progressTo) — сами дважды
// уровень не проверяют, кроме haptic (она работает на ВСЕХ уровнях, включая "off" — это не
// визуальное движение).
//
// Порядок определения уровня — ровно такой и никакой другой (UI-SPEC §Motion, D-17 уточнение
// от 23.08, дополнено quick 260915-4mw четвёртым сигналом):
//   1. prefers-reduced-motion — CSS-медиа-запрос, это ПОЛ: срабатывает -> "off" безусловно,
//      ничего ниже уже не переопределяет (даже если менеджер выбрал "auto").
//   2. Низкий заряд батареи (<20%, не заряжается) — прокси энергосбережения ОС; сам режим
//      энергосбережения веб-платформа не отдаёт, это осознанная неточность. Battery API нет
//      на iOS и снят в Firefox — отказ проверки НИКОГДА не блокирует отрисовку (.catch()).
//   3. Android (tg.platform === "android") + hardwareConcurrency<=4 — гейт ТОЛЬКО на Android:
//      на iOS hardwareConcurrency спуфится константой (обычно 4) ради защиты от отпечатка,
//      без гейта каждый iPhone получал бы заниженный уровень.
//   4. Настройка менеджера (miniapp_motion, "auto"/"micro"/"off") — ПОТОЛОК поверх результата
//      шагов 2-3: берётся более слабый из авто-уровня и настройки (ранги full=2, micro=1,
//      off=0). Варианта «принудительно full» в реестре нет НАМЕРЕННО — он снял бы защиту
//      слабых Android в Telegram WebView (шаг 3); менеджер может только ОСЛАБИТЬ движение,
//      никогда не усилить его выше того, что уже посчитала автологика.

const TIER_RANK = { off: 0, micro: 1, full: 2 };

/**
 * Чистая функция принятия решения по четырём собранным сигналам — отделена от сбора сигналов,
 * чтобы порядок можно было прочитать и проверить глазами (и статическим тестом). Неизвестное
 * или отсутствующее `setting` трактуется как "auto" (ничего не ослабляет).
 */
export function resolveMotionTier({ reduced, lowBattery, lowCoresAndroid, setting }) {
  if (reduced) return "off";
  const autoTier = (lowBattery || lowCoresAndroid) ? "micro" : "full";
  const normalizedSetting = Object.prototype.hasOwnProperty.call(TIER_RANK, setting) ? setting : "auto";
  if (normalizedSetting === "auto") return autoTier;
  return TIER_RANK[normalizedSetting] < TIER_RANK[autoTier] ? normalizedSetting : autoTier;
}

function detectReduced() {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function detectLowCoresAndroid(tg) {
  return Boolean(
    tg && tg.platform === "android" &&
    typeof navigator !== "undefined" && navigator.hardwareConcurrency <= 4,
  );
}

function detectLowBattery() {
  if (typeof navigator === "undefined" || typeof navigator.getBattery !== "function") {
    return Promise.resolve(false);
  }
  return navigator.getBattery()
    .then((battery) => !battery.charging && battery.level < 0.2)
    .catch(() => false); // отказ (Firefox без API, sandbox и т.п.) — не блокирует отрисовку
}

/**
 * Собирает сигналы и пишет результат в document.documentElement.dataset.motion. Синхронная
 * часть (reduced-motion + Android/ядра + настройка менеджера) ставится СРАЗУ, до первой
 * отрисовки; батарейное уточнение — асинхронное понижение уровня уже после первого кадра.
 * Настройка менеджера едет атрибутом `<body data-motion-setting>` (не полем /app/api/me,
 * quick 260915-4mw, gotcha 5) — уровень должен быть готов ДО первой отрисовки экрана.
 */
export function applyMotionTier() {
  const tg = window.Telegram && window.Telegram.WebApp;
  const reduced = detectReduced();
  const lowCoresAndroid = detectLowCoresAndroid(tg);
  const setting = (document.body && document.body.dataset.motionSetting) || "auto";
  const root = document.documentElement;
  root.dataset.motion = resolveMotionTier({ reduced, lowBattery: false, lowCoresAndroid, setting });
  if (reduced) return; // "off" ничем ниже не переопределяется — батарею можно не спрашивать
  detectLowBattery().then((lowBattery) => {
    if (!lowBattery) return;
    root.dataset.motion = resolveMotionTier({ reduced, lowBattery, lowCoresAndroid, setting });
  });
}

/**
 * Единственное место чтения `dataset.motion` за пределами `applyMotionTier` (quick
 * 260915-4mw) — раньше строка `document.documentElement.dataset.motion` жила в файле четырьмя
 * копиями (confetti/countUp плюс будущие slideIn/stagger/progressTo), теперь один читатель.
 */
export function motionTier() {
  return document.documentElement.dataset.motion;
}

/**
 * Одноразовый ~1200мс залп на <canvas>, вставленном внутрь target (top-1 / принятая сдача).
 * Сам себя убирает из DOM. На уровнях "micro"/"off" не запускается — проверяет dataset.motion
 * сам, вызывающая сторона не обязана помнить об уровне.
 */
export function confetti(target) {
  if (!target || motionTier() !== "full") return;
  const canvas = document.createElement("canvas");
  const width = target.clientWidth || 320;
  const height = target.clientHeight || 160;
  canvas.width = width;
  canvas.height = height;
  canvas.style.position = "absolute";
  canvas.style.inset = "0";
  canvas.style.pointerEvents = "none";
  if (getComputedStyle(target).position === "static") target.style.position = "relative";
  target.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    canvas.remove();
    return;
  }
  const particles = Array.from({ length: 28 }, () => ({
    x: width / 2,
    y: height / 2,
    vx: (Math.random() - 0.5) * 7,
    vy: (Math.random() - 1.6) * 6,
    size: 3 + Math.random() * 3,
    hue: Math.floor(Math.random() * 360),
  }));
  const duration = 1200;
  const startedAt = performance.now();
  function frame(now) {
    const elapsed = now - startedAt;
    const progress = Math.min(1, elapsed / duration);
    ctx.clearRect(0, 0, width, height);
    ctx.globalAlpha = 1 - progress;
    for (const p of particles) {
      const x = p.x + p.vx * elapsed * 0.06;
      const y = p.y + p.vy * elapsed * 0.06 + 0.0009 * elapsed * elapsed;
      ctx.fillStyle = `hsl(${p.hue} 80% 55%)`;
      ctx.fillRect(x, y, p.size, p.size);
    }
    if (progress < 1) requestAnimationFrame(frame);
    else canvas.remove();
  }
  requestAnimationFrame(frame);
}

/**
 * Докрутка числа: 600–800мс на "full", 400мс на "micro", мгновенная установка на "off".
 * Число рисуется через textContent.
 */
export function countUp(el, from, to) {
  if (!el) return;
  const target = Math.round(to);
  const tier = motionTier();
  if (tier === "off") {
    el.textContent = String(target);
    return;
  }
  const duration = tier === "micro" ? 400 : 700;
  const start = Math.round(from);
  const delta = target - start;
  const startedAt = performance.now();
  function frame(now) {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - (1 - progress) ** 3;
    el.textContent = String(Math.round(start + delta * eased));
    if (progress < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

/**
 * Обёртка над tg.HapticFeedback. Работает на ВСЕХ уровнях motion, включая "off" — хаптика не
 * визуальное движение (D-17). Отсутствие API — тихий no-op.
 */
export function haptic(kind) {
  const tg = window.Telegram && window.Telegram.WebApp;
  const feedback = tg && tg.HapticFeedback;
  if (!feedback) return;
  try {
    if (kind === "success" && typeof feedback.notificationOccurred === "function") {
      feedback.notificationOccurred("success");
    } else if (kind === "light" && typeof feedback.impactOccurred === "function") {
      feedback.impactOccurred("light");
    }
  } catch (_) { /* конкретный клиент без метода — не критично */ }
}

/**
 * Направленный въезд ОДНОГО узла (quick 260915-4mw, ANIM-01/02): переходы между экранами
 * (app.js::route) и смена шага анкеты (form.js::drawStep) анимируют ТОЛЬКО входящий контент —
 * решение и его причина закомментированы в app.js рядом с вызовом. На "off" элемент не
 * трогается вовсе — ни один класс, ни один inline-стиль (проверено тестом «off ничего не
 * добавляет»). На "full"/"micro" ставится смещённое/прозрачное стартовое состояние классом
 * без перехода (`.m-from-right`/`.m-from-left`, `transition: none` в app.css), принудительный
 * reflow гарантирует, что браузер это состояние увидел, затем снятие класса направления даёт
 * `.m-anim` доехать transition'ом до нормального вида. `m-anim` снимается по `transitionend`
 * (once) — а на случай свёрнутой вкладки (событие не придёт) страховочным таймером 400мс,
 * дольше самого длинного `--dur-enter` с запасом.
 */
export function slideIn(el, direction = "fwd") {
  if (!el || motionTier() === "off") return;
  const dirClass = direction === "back" ? "m-from-left" : "m-from-right";
  el.classList.add("m-anim", dirClass);
  void el.offsetWidth; // форсируем рефлоу — браузер обязан зафиксировать стартовое состояние
  el.classList.remove(dirClass);
  let done = false;
  function cleanup() {
    if (done) return;
    done = true;
    el.classList.remove("m-anim");
  }
  el.addEventListener("transitionend", cleanup, { once: true });
  setTimeout(cleanup, 400);
}

/**
 * Лесенка появления: первым `max` (по умолчанию 8, D-Motion — «не более 8 строк подряд»)
 * детям контейнера начиная с индекса `from` вешает класс `m-rise` с растущей задержкой
 * (`step` мс на элемент, по умолчанию 36мс — то же число, что `--dur-stagger-step` в
 * app.css). На "off" не делает ничего. Задержка и класс снимаются по `animationend` (once) —
 * никакого таймера-страховки: `animation` (в отличие от `transition`) не зависит от того,
 * какое конкретно свойство меняется, событие приходит надёжно.
 */
export function stagger(container, { from = 0, max = 8, step = 36 } = {}) {
  if (!container || motionTier() === "off") return;
  const children = Array.from(container.children).slice(from, from + max);
  children.forEach((child, i) => {
    child.classList.add("m-rise");
    child.style.animationDelay = `${i * step}ms`;
    child.addEventListener("animationend", () => {
      child.classList.remove("m-rise");
      child.style.animationDelay = "";
    }, { once: true });
  });
}

/**
 * Полоса прогресса доезжает трансформом (D-Motion, ограничение владельца №3 — не анимировать
 * `width`, layout-свойство). Значения зажаты в [0,1]. На "off" сразу ставит конечный `scaleX`
 * без промежуточного состояния — это финальное состояние полосы, не движение (verification
 * §3: единственное исключение из инварианта «off ничего не добавляет»). Иначе ставит стартовый
 * `scaleX(from)`, рефлоу фиксирует его, а в следующем кадре (один `rAF`, ничего больше в этом
 * примитиве не ждёт кадра) — конечный `scaleX(to)`, чтобы CSS-transition `.wizard-progress-fill`
 * успел увидеть смену значения и доехать, а не перепрыгнуть.
 */
export function progressTo(fill, from, to) {
  if (!fill) return;
  const clamp = (v) => Math.max(0, Math.min(1, v));
  const target = clamp(to);
  if (motionTier() === "off") {
    fill.style.transform = `scaleX(${target})`;
    return;
  }
  const start = clamp(from);
  fill.style.transform = `scaleX(${start})`;
  void fill.offsetWidth;
  requestAnimationFrame(() => {
    fill.style.transform = `scaleX(${target})`;
  });
}
