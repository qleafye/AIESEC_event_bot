// Motion (D-17): единственное место, где живёт знание об уровнях движения. Экраны и ядро
// зовут готовые помощники (confetti/countUp/haptic) — сами дважды уровень не проверяют,
// кроме haptic (она работает на ВСЕХ уровнях, включая "off" — это не визуальное движение).
//
// Порядок определения уровня — ровно такой и никакой другой (UI-SPEC §Motion, D-17 уточнение
// от 23.08):
//   1. prefers-reduced-motion — CSS-медиа-запрос, это ПОЛ: срабатывает -> "off" безусловно,
//      ничего ниже уже не переопределяет.
//   2. Низкий заряд батареи (<20%, не заряжается) — прокси энергосбережения ОС; сам режим
//      энергосбережения веб-платформа не отдаёт, это осознанная неточность. Battery API нет
//      на iOS и снят в Firefox — отказ проверки НИКОГДА не блокирует отрисовку (.catch()).
//   3. Android (tg.platform === "android") + hardwareConcurrency<=4 — гейт ТОЛЬКО на Android:
//      на iOS hardwareConcurrency спуфится константой (обычно 4) ради защиты от отпечатка,
//      без гейта каждый iPhone получал бы заниженный уровень.
//   4. Иначе — "full".

/**
 * Чистая функция принятия решения по трём собранным сигналам — отделена от сбора сигналов,
 * чтобы порядок можно было прочитать и проверить глазами (и статическим тестом).
 */
export function resolveMotionTier({ reduced, lowBattery, lowCoresAndroid }) {
  if (reduced) return "off";
  if (lowBattery || lowCoresAndroid) return "micro";
  return "full";
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
 * часть (reduced-motion + Android/ядра) ставится СРАЗУ, до первой отрисовки; батарейное
 * уточнение — асинхронное понижение уровня уже после первого кадра.
 */
export function applyMotionTier() {
  const tg = window.Telegram && window.Telegram.WebApp;
  const reduced = detectReduced();
  const lowCoresAndroid = detectLowCoresAndroid(tg);
  const root = document.documentElement;
  root.dataset.motion = resolveMotionTier({ reduced, lowBattery: false, lowCoresAndroid });
  if (reduced) return; // "off" ничем ниже не переопределяется — батарею можно не спрашивать
  detectLowBattery().then((lowBattery) => {
    if (!lowBattery) return;
    root.dataset.motion = resolveMotionTier({ reduced, lowBattery, lowCoresAndroid });
  });
}

/**
 * Одноразовый ~1200мс залп на <canvas>, вставленном внутрь target (top-1 / принятая сдача).
 * Сам себя убирает из DOM. На уровнях "micro"/"off" не запускается — проверяет dataset.motion
 * сам, вызывающая сторона не обязана помнить об уровне.
 */
export function confetti(target) {
  if (!target || document.documentElement.dataset.motion !== "full") return;
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
  const tier = document.documentElement.dataset.motion;
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
