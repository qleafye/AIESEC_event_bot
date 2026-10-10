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

// Сколько пикселей можно декодировать целиком. 12–24 Мп (обычный снимок) проходят; 48–108 Мп
// целиком в память не берём — iOS-вебвью от такого падает целиком, и fail-soft это не ловит.
// Крупные JPEG/PNG декодируются сразу в целевой размер (resizeWidth/resizeHeight), а там, где
// размер заранее не узнать или его нельзя безопасно передать, — уходит оригинал.
export const MAX_DECODE_PIXELS = 24000000;
const HEADER_BYTES = 256 * 1024;

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

// ── размеры из заголовка файла, без декодирования ─────────────────────────────────────────
// JPEG: SOF-маркер (ширина/высота как записаны) + EXIF Orientation (5–8 — снимок повёрнут на
// 90°, видимые стороны меняются местами). PNG: IHDR. Прочее (HEIC, WebP, …) — неизвестно.

function exifOrientation(dv, start) {
  if (start + 14 > dv.byteLength || dv.getUint32(start) !== 0x45786966) return 0; // "Exif"
  const tiff = start + 6;
  const little = dv.getUint16(tiff) === 0x4949;
  const ifd = tiff + dv.getUint32(tiff + 4, little);
  if (ifd + 2 > dv.byteLength) return 0;
  const count = dv.getUint16(ifd, little);
  for (let i = 0; i < count; i += 1) {
    const entry = ifd + 2 + i * 12;
    if (entry + 12 > dv.byteLength) return 0;
    if (dv.getUint16(entry, little) === 0x0112) return dv.getUint16(entry + 8, little);
  }
  return 0;
}

function jpegInfo(dv) {
  let off = 2;
  let orientation = 1;
  while (off + 4 <= dv.byteLength) {
    if (dv.getUint8(off) !== 0xFF) return null;
    const marker = dv.getUint8(off + 1);
    if (marker === 0xFF) { off += 1; continue; }
    if (marker === 0x01 || (marker >= 0xD0 && marker <= 0xD8)) { off += 2; continue; }
    if (marker === 0xDA || marker === 0xD9) return null;
    const len = dv.getUint16(off + 2);
    if (len < 2) return null;
    if (marker === 0xE1) orientation = exifOrientation(dv, off + 4) || orientation;
    const isSof = marker >= 0xC0 && marker <= 0xCF && marker !== 0xC4 && marker !== 0xC8 && marker !== 0xCC;
    if (isSof) {
      if (off + 9 > dv.byteLength) return null;
      return { width: dv.getUint16(off + 7), height: dv.getUint16(off + 5), orientation };
    }
    off += 2 + len;
  }
  return null;
}

async function readImageInfo(file) {
  try {
    if (typeof file.slice !== "function") return null;
    const dv = new DataView(await file.slice(0, HEADER_BYTES).arrayBuffer());
    if (dv.byteLength >= 24 && dv.getUint32(0) === 0x89504E47) {
      return { width: dv.getUint32(16), height: dv.getUint32(20), orientation: 1 };
    }
    if (dv.byteLength >= 4 && dv.getUint16(0) === 0xFFD8) return jpegInfo(dv);
  } catch (_) {
    // битый заголовок — как неизвестный формат
  }
  return null;
}

// <img> декодирует то, что умеет движок, и сам учитывает EXIF-поворот (CSS
// image-orientation: from-image — значение по умолчанию во всех живых движках). После
// onload размеры уже известны, а пиксели браузер декодирует только при отрисовке — слишком
// крупную картинку здесь отсекаем, не нарисовав.
function decodeWithImg(file) {
  if (typeof Image !== "function" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return Promise.resolve(null);
  }
  const url = URL.createObjectURL(file);
  return new Promise((resolve) => {
    const img = new Image();
    const done = (value) => { URL.revokeObjectURL(url); resolve(value); };
    img.onload = () => {
      const w = img.naturalWidth;
      const h = img.naturalHeight;
      done(w && h && w * h <= MAX_DECODE_PIXELS ? img : null);
    };
    img.onerror = () => done(null);
    img.src = url;
  });
}

// createImageBitmap с явным imageOrientation: "from-image" — поворот по EXIF (фото с телефона,
// снятое вертикально, иначе легло бы набок). Размеры известны из заголовка — декодируем сразу
// в целевой размер (resizeWidth/resizeHeight), полный кадр в память не попадает. У снимка,
// повёрнутого на 90° (EXIF 5–8), движки по-разному понимают, к какой ориентации относится
// resize, — такой декодируем без resize и только если он не крупнее MAX_DECODE_PIXELS.
// Размеров нет, движок не знает значения или не декодирует формат — <img> с тем же потолком.
async function decode(file, maxSide) {
  const info = await readImageInfo(file);
  if (info && typeof globalThis.createImageBitmap === "function") {
    const rotated = info.orientation >= 5 && info.orientation <= 8;
    const scale = Math.min(1, maxSide / Math.max(info.width, info.height));
    let opts = null;
    if (scale < 1 && !rotated) {
      opts = {
        imageOrientation: "from-image",
        resizeWidth: Math.max(1, Math.round(info.width * scale)),
        resizeHeight: Math.max(1, Math.round(info.height * scale)),
        resizeQuality: "high",
      };
    } else if (info.width * info.height <= MAX_DECODE_PIXELS) {
      opts = { imageOrientation: "from-image" };
    }
    if (!opts) return null;
    try {
      return await globalThis.createImageBitmap(file, opts);
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
    source = await decode(file, maxSide);
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
// Вытесняется самый давно ПОКАЗАННЫЙ, а не самый давно загруженный: обложка черновика
// перерисовывается на каждом шаге и поэтому не пропадает, сколько бы фото ни ушло в сдачи.
const LOCAL_LIMIT = 12;
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
  const url = (fileId && localUrls.get(fileId)) || null;
  if (url) {
    localUrls.delete(fileId);
    localUrls.set(fileId, url);
  }
  return url;
}
