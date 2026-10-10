// Фото перед загрузкой в Mini App: ужать на телефоне и показать превью из локального файла.
//
// Зачем (приёмка 10.10): обложка задания с камеры весит 1–5 МБ и проходит туннель и прокси
// до Telegram, а превью потом скачивалось с сервера заново. Здесь картинка уменьшается до
// длинной стороны MAX_SIDE и перекодируется в JPEG — до сети уходит 200–400 КБ, а превью
// берётся из того же локального файла (`rememberLocalPhoto` / `localPhotoUrl`).
//
// Fail-soft: не смогли декодировать (HEIC в Chrome на Android, старый движок), нет canvas,
// ужатый файл вышел не меньше оригинала — возвращается оригинал как есть, загрузка идёт
// прежним путём. GIF (анимация) и SVG не трогаются никогда.
//
// Без внешних библиотек и без DOM-хелперов проекта: модуль гоняется в node-тесте
// (tests/test_miniapp_photo_shrink_js_261011.py) с фейковыми createImageBitmap/canvas.

export const MAX_SIDE = 1600;
// Ассеты оформления (постер, фото программы с мелким текстом): тот же потолок, что Telegram
// хранит у фото сам, — сжатие не делает их хуже того, что покажет бот.
export const MAX_SIDE_ASSET = 2560;
export const JPEG_QUALITY = 0.82;
// Потолок на одно сжатие. Часть вебвью на HEIC не присылает ни onload, ни onerror, и
// createImageBitmap тоже может не ответить никогда — без потолка промис висел бы, а общая
// очередь держала бы все следующие фото до конца сессии. По таймауту уходит оригинал.
export const SHRINK_TIMEOUT_MS = 12000;

const KEEP_AS_IS = new Set(["image/gif", "image/svg+xml"]);
const IMAGE_EXT_RE = /\.(jpe?g|png|webp|heic|heif|avif|bmp)$/i;

function looksLikeImage(file) {
  const type = String(file.type || "").toLowerCase();
  if (type) return type.startsWith("image/") && !KEEP_AS_IS.has(type);
  return IMAGE_EXT_RE.test(String(file.name || ""));
}

function jpegName(name) {
  const base = String(name || "").replace(/\.[^./\\]{1,8}$/, "");
  return `${base || "photo"}.jpg`;
}

// <img> декодирует то, что умеет движок, и сам учитывает EXIF-поворот (CSS
// image-orientation: from-image — значение по умолчанию во всех живых движках).
function decodeWithImg(file) {
  if (typeof Image !== "function" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return Promise.resolve(null);
  }
  const url = URL.createObjectURL(file);
  return new Promise((resolve) => {
    const img = new Image();
    const done = (value) => { URL.revokeObjectURL(url); resolve(value); };
    img.onload = () => done(img.naturalWidth && img.naturalHeight ? img : null);
    img.onerror = () => done(null);
    img.src = url;
  });
}

// createImageBitmap с явным imageOrientation: "from-image" — поворот по EXIF (фото с телефона,
// снятое вертикально, иначе легло бы набок). Движок, который не знает значения или не
// декодирует формат, бросает — тогда <img>.
async function decode(file) {
  if (typeof globalThis.createImageBitmap === "function") {
    try {
      return await globalThis.createImageBitmap(file, { imageOrientation: "from-image" });
    } catch (_) {
      // ниже — <img>
    }
  }
  return decodeWithImg(file);
}

function makeCanvas(width, height) {
  if (typeof document !== "undefined" && typeof document.createElement === "function") {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    return canvas;
  }
  if (typeof OffscreenCanvas === "function") return new OffscreenCanvas(width, height);
  return null;
}

function canvasToJpeg(canvas, quality) {
  if (typeof canvas.convertToBlob === "function") return canvas.convertToBlob({ type: "image/jpeg", quality });
  if (typeof canvas.toBlob !== "function") return Promise.resolve(null);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
}

async function shrinkNow(file, maxSide, quality) {
  if (!file || typeof file.size !== "number" || !looksLikeImage(file)) return file;
  let source = null;
  let canvas = null;
  try {
    source = await decode(file);
    if (!source) return file;
    const srcW = source.naturalWidth || source.width;
    const srcH = source.naturalHeight || source.height;
    if (!srcW || !srcH) return file;
    const scale = Math.min(1, maxSide / Math.max(srcW, srcH));
    const width = Math.max(1, Math.round(srcW * scale));
    const height = Math.max(1, Math.round(srcH * scale));
    canvas = makeCanvas(width, height);
    const ctx = canvas && canvas.getContext("2d");
    if (!ctx) return file;
    // У JPEG нет прозрачности: без подложки прозрачный PNG стал бы чёрным.
    ctx.fillStyle = "white";
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(source, 0, 0, width, height);
    const blob = await canvasToJpeg(canvas, quality);
    if (!blob || blob.type !== "image/jpeg" || blob.size >= file.size) return file;
    return new File([blob], jpegName(file.name), { type: "image/jpeg", lastModified: Date.now() });
  } catch (_) {
    return file;
  } finally {
    if (source && typeof source.close === "function") source.close();
    // iOS держит память canvas до сборки мусора — обнуляем размер сразу.
    if (canvas) { canvas.width = 0; canvas.height = 0; }
  }
}

// Не дождались — `fallback`. Зависшее сжатие доработает (или не доработает) в фоне, его
// результат никто не ждёт; очередь идёт дальше.
function withTimeout(promise, ms, fallback) {
  let timer = null;
  const expired = new Promise((resolve) => { timer = setTimeout(() => resolve(fallback), ms); });
  return Promise.race([promise, expired]).finally(() => clearTimeout(timer));
}

// Фото сжимаются по одному: десять снимков, выбранных разом, не декодируются в память
// одновременно (на слабом телефоне это вылет вебвью). Сами загрузки по-прежнему параллельны.
let queue = Promise.resolve();

/**
 * Ужатая копия фото (JPEG, длинная сторона ≤ maxSide) или сам `file`, если сжимать нечего
 * или не получилось. Никогда не бросает.
 * @param {File} file
 * @param {{maxSide?: number, quality?: number, timeoutMs?: number}} [opts]
 * @returns {Promise<File>}
 */
export function shrinkPhoto(file, { maxSide = MAX_SIDE, quality = JPEG_QUALITY, timeoutMs = SHRINK_TIMEOUT_MS } = {}) {
  // PDF/документ, выбранный вместе с фото, не ждёт в очереди их сжатия.
  if (!file || typeof file.size !== "number" || !looksLikeImage(file)) return Promise.resolve(file);
  const run = queue.then(() => withTimeout(shrinkNow(file, maxSide, quality), timeoutMs, file));
  queue = run.catch(() => null);
  return run;
}

// ── превью из локального файла ────────────────────────────────────────────────────────────
// file_id, только что загруженный с этого телефона -> object URL того же файла. Экран
// показывает превью сразу, без GET /api/file (getFile + скачивание через прокси и туннель).
// Живёт между экранами (мастер создания -> карточка задания), старые URL освобождаются.
const LOCAL_LIMIT = 6;
const localUrls = new Map();

export function forgetLocalPhoto(fileId) {
  const url = localUrls.get(fileId);
  if (!url) return;
  localUrls.delete(fileId);
  URL.revokeObjectURL(url);
}

export function rememberLocalPhoto(fileId, file) {
  if (!fileId || !file || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return null;
  forgetLocalPhoto(fileId);
  const url = URL.createObjectURL(file);
  localUrls.set(fileId, url);
  while (localUrls.size > LOCAL_LIMIT) forgetLocalPhoto(localUrls.keys().next().value);
  return url;
}

export function localPhotoUrl(fileId) {
  return (fileId && localUrls.get(fileId)) || null;
}
