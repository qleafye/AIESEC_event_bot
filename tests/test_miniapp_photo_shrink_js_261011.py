"""Приёмка 10.10: фото к заданию в Mini App грузилось очень долго — оригинал с камеры (1,4 МБ)
шёл через туннель и прокси, а превью потом трижды скачивалось с сервера.

`miniapp/static/js/photo_shrink.js` ужимает фото на телефоне (JPEG, длинная сторона ≤1600,
EXIF-поворот через `imageOrientation: "from-image"`, fail-soft на оригинал) и хранит
локальное превью по `file_id`. Поведение гоняется в node с фейковыми
createImageBitmap/canvas/Image (тот же приём, что `tests/test_miniapp_net_health_js_260925.py`);
без node в PATH — skip с причиной.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import json
import shutil
import subprocess

import pytest

from tests.test_miniapp_frontend import _HEX_OR_RGB_COLOR, _js_without_comments

ROOT = REPO_ROOT
SHRINK_JS = ROOT / "miniapp" / "static" / "js" / "photo_shrink.js"

NODE_SCRIPT = r"""
const DIMS = {};            // имя файла -> [w, h] (видимые) у фейкового декодера без resize
const BAD = new Set();      // имена, на которых createImageBitmap бросает
const HANG = new Set();     // имена, на которых декодер не отвечает никогда
let OUT_SIZE = 300000;      // размер JPEG, который «выдаёт» canvas
let OUT_TYPE = "image/jpeg";
const log = { opts: [], draws: [], fills: [], quality: [], closed: 0, cibCalls: 0, active: 0, maxActive: 0, revoked: [] };

globalThis.createImageBitmap = async (file, opts) => {
  if (HANG.has(file.name)) return new Promise(() => {});
  log.cibCalls += 1;
  log.opts.push(opts);
  log.active += 1;
  log.maxActive = Math.max(log.maxActive, log.active);
  await new Promise((ok) => setTimeout(ok, 5));
  log.active -= 1;
  if (BAD.has(file.name)) throw new Error("cannot decode");
  const [width, height] = opts && opts.resizeWidth ? [opts.resizeWidth, opts.resizeHeight] : DIMS[file.name];
  return { width, height, close() { log.closed += 1; } };
};

globalThis.document = {
  createElement(tag) {
    return {
      tag, width: 0, height: 0,
      getContext() {
        return {
          set fillStyle(v) { log.fills.push(v); },
          fillRect() {},
          drawImage(src, x, y, w, h) { log.draws.push([w, h]); },
        };
      },
      toBlob(cb, type, quality) {
        log.quality.push([type, quality]);
        cb(new Blob([new Uint8Array(OUT_SIZE)], { type: OUT_TYPE }));
      },
    };
  },
};

const realRevoke = URL.revokeObjectURL.bind(URL);
URL.revokeObjectURL = (u) => { log.revoked.push(u); realRevoke(u); };

const m = await import(%(url)s);
const pad = (head, size) => { const b = new Uint8Array(Math.max(size, head.length)); b.set(head); return b; };
const file = (name, size, type) => new File([new Uint8Array(size)], name, { type });
// Минимальный JPEG-заголовок: SOI, APP1 c EXIF Orientation (big-endian TIFF), SOF0.
function jpg(name, size, w, h, orientation = 0, type = "image/jpeg") {
  const bytes = [0xFF, 0xD8];
  if (orientation) {
    const exif = [0x45, 0x78, 0x69, 0x66, 0, 0, 0x4D, 0x4D, 0, 0x2A, 0, 0, 0, 8, 0, 1,
      0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, orientation, 0, 0, 0, 0, 0, 0];
    const len = exif.length + 2;
    bytes.push(0xFF, 0xE1, len >> 8, len & 255, ...exif);
  }
  bytes.push(0xFF, 0xC0, 0, 17, 8, h >> 8, h & 255, w >> 8, w & 255, 3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1);
  return new File([pad(bytes, size)], name, { type });
}
function png(name, size, w, h) {
  const head = new Uint8Array(24);
  const dv = new DataView(head.buffer);
  dv.setUint32(0, 0x89504E47); dv.setUint32(4, 0x0D0A1A0A); dv.setUint32(8, 13); dv.setUint32(12, 0x49484452);
  dv.setUint32(16, w); dv.setUint32(20, h);
  return new File([pad(head, size)], name, { type: "image/png" });
}
const r = {};

// 1. альбомное фото с камеры 4000×3000, 1,4 МБ -> декодируется сразу в 1600×1200
{
  const out = await m.shrinkPhoto(jpg("IMG_0001.JPG", 1435564, 4000, 3000));
  r.landscape = { name: out.name, type: out.type, size: out.size, draw: log.draws.at(-1),
    opts: log.opts.at(-1), quality: log.quality.at(-1), fill: log.fills.at(-1), closed: log.closed };
}
// 2. вертикальное без EXIF-поворота 3000×4000 -> 1200×1600
{ await m.shrinkPhoto(jpg("portrait.jpeg", 2000000, 3000, 4000)); r.portrait = [log.draws.at(-1), log.opts.at(-1).resizeWidth]; }
// 2b. вертикальный снимок iPhone: пиксели 4032×3024 + EXIF 6 -> без resize, видимые 3024×4032
DIMS["rotated.jpg"] = [3024, 4032];
{ await m.shrinkPhoto(jpg("rotated.jpg", 2500000, 4032, 3024, 6)); r.rotated = [log.draws.at(-1), log.opts.at(-1)]; }
// 3. маленький PNG, JPEG вышел больше -> оригинал; не растягивается, resize не просим
DIMS["shot.png"] = [800, 600];
{
  OUT_SIZE = 90000;
  const f = png("shot.png", 50000, 800, 600);
  r.biggerKeepsOriginal = (await m.shrinkPhoto(f)) === f;
  r.smallNotUpscaled = [log.draws.at(-1), log.opts.at(-1)];
  OUT_SIZE = 300000;
}
// 4. GIF не трогаем и не декодируем
{
  const before = log.cibCalls;
  const f = file("anim.gif", 3000000, "image/gif");
  r.gifUntouched = (await m.shrinkPhoto(f)) === f && log.cibCalls === before;
}
// 5. не изображение -> как есть
{ const f = file("doc.pdf", 3000000, "application/pdf"); r.pdfUntouched = (await m.shrinkPhoto(f)) === f; }
// 6. формат без известного заголовка, <img> нет -> оригинал
{ const f = file("broken.heic", 3000000, "image/heic"); r.decodeFailKeepsOriginal = (await m.shrinkPhoto(f)) === f; }
// 7. HEIC, который <img> декодирует (Safari) -> .jpg
{
  globalThis.Image = class {
    set src(u) { this.naturalWidth = 4032; this.naturalHeight = 3024; setTimeout(() => this.onload(), 0); }
  };
  const out = await m.shrinkPhoto(file("IMG_2.HEIC", 2500000, "image/heic"));
  r.heic = [out.name, out.type, log.draws.at(-1)];
  delete globalThis.Image;
}
// 8. canvas не умеет JPEG (вернул PNG) -> оригинал
{
  OUT_TYPE = "image/png";
  const f = jpg("nojpeg.jpg", 2000000, 4000, 3000);
  r.noJpegKeepsOriginal = (await m.shrinkPhoto(f)) === f;
  OUT_TYPE = "image/jpeg";
}
// 9. фолбэк на <img>, когда createImageBitmap бросает (старый движок)
{
  BAD.add("legacy.jpg");
  globalThis.Image = class {
    set src(u) { this.naturalWidth = 2000; this.naturalHeight = 1000; setTimeout(() => this.onload(), 0); }
  };
  const out = await m.shrinkPhoto(jpg("legacy.jpg", 2000000, 2000, 1000));
  r.imgFallback = [out.name, log.draws.at(-1)];
  delete globalThis.Image;
}
// 10. ассет оформления — потолок 2560
{ await m.shrinkPhoto(jpg("poster.jpg", 4000000, 5000, 2500), { maxSide: m.MAX_SIDE_ASSET }); r.asset = log.draws.at(-1); }
// 11. три фото разом — декодируются по одному
{
  log.maxActive = 0;
  const outs = await Promise.all(["a.jpg", "b.jpg", "c.jpg"].map((n) => m.shrinkPhoto(jpg(n, 2000000, 4000, 3000))));
  r.serial = log.maxActive;
  r.parallelNames = outs.map((o) => o.name);
}
// 12. превью по file_id: запомнить, отдать, вытеснить старое с revoke
{
  const shrunk = file("x.jpg", 1000, "image/jpeg");
  const url = m.rememberLocalPhoto("FILE_A", shrunk);
  r.localUrl = [url.startsWith("blob:"), m.localPhotoUrl("FILE_A") === url, m.localPhotoUrl("UNKNOWN")];
  const replaced = m.rememberLocalPhoto("FILE_A", shrunk);
  r.replaceRevokesOld = log.revoked.includes(url) && m.localPhotoUrl("FILE_A") === replaced;
  // показанная обложка черновика переживает 12 новых фото, непоказанная — вытесняется
  const draft = m.rememberLocalPhoto("DRAFT", shrunk);
  for (let i = 0; i < 12; i += 1) { m.rememberLocalPhoto(`F${i}`, shrunk); m.localPhotoUrl("DRAFT"); }
  r.evicted = [m.localPhotoUrl("FILE_A"), log.revoked.includes(replaced), m.localPhotoUrl("F11") !== null];
  r.draftKept = m.localPhotoUrl("DRAFT") === draft && !log.revoked.includes(draft);
  m.forgetLocalPhoto("F11");
  r.forgotten = m.localPhotoUrl("F11");
  r.nullSafe = [m.rememberLocalPhoto(null, shrunk), m.localPhotoUrl(null)];
}
// 13. декодер завис: по таймауту — оригинал, следующее фото очередь не держит
{
  HANG.add("hang.jpg");
  const f = jpg("hang.jpg", 3000000, 4000, 3000);
  const t0 = Date.now();
  const out = await m.shrinkPhoto(f, { timeoutMs: 50 });
  r.hangKeepsOriginal = out === f;
  const next = await m.shrinkPhoto(jpg("after.jpg", 2000000, 4000, 3000));
  r.queueFreed = [next.name, next.type, Date.now() - t0 < 5000];
}
// 14. <img> не прислал ни onload, ни onerror
{
  globalThis.Image = class { set src(u) { /* тишина */ } };
  const f = file("silent.heic", 3000000, "image/heic");
  r.silentImgKeepsOriginal = (await m.shrinkPhoto(f, { timeoutMs: 50 })) === f;
  delete globalThis.Image;
  r.queueFreed2 = (await m.shrinkPhoto(jpg("after2.jpg", 2000000, 4000, 3000))).name;
}
// 15. 108 Мп без поворота -> декодируется сразу в 1600×1200, полный кадр не просим
{ await m.shrinkPhoto(jpg("huge.jpg", 9000000, 12000, 9000)); r.huge = [log.opts.at(-1), log.draws.at(-1)]; }
// 16. 108 Мп с EXIF-поворотом -> оригинал, декодер не зовём
{
  const before = log.cibCalls;
  const f = jpg("huge_rot.jpg", 9000000, 12000, 9000, 6);
  r.hugeRotated = [(await m.shrinkPhoto(f)) === f, log.cibCalls === before];
}
// 17. 108 Мп неизвестного формата: <img> знает размеры, но рисовать не даём -> оригинал
{
  const drawsBefore = log.draws.length;
  globalThis.Image = class {
    set src(u) { this.naturalWidth = 12000; this.naturalHeight = 9000; setTimeout(() => this.onload(), 0); }
  };
  const f = file("huge.heic", 9000000, "image/heic");
  r.hugeUnknown = [(await m.shrinkPhoto(f)) === f, log.draws.length === drawsBefore];
  delete globalThis.Image;
}
console.log(JSON.stringify(r));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест photo_shrink пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(SHRINK_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_camera_photo_becomes_small_jpeg_with_exif_orientation(result):
    got = result["landscape"]
    assert got["name"] == "IMG_0001.jpg" and got["type"] == "image/jpeg"
    assert got["size"] == 300000
    assert got["draw"] == [1600, 1200]
    # декодируется сразу в целевой размер, поворот по EXIF
    assert got["opts"] == {"imageOrientation": "from-image", "resizeWidth": 1600, "resizeHeight": 1200, "resizeQuality": "high"}
    assert got["quality"] == ["image/jpeg", 0.82]
    assert got["fill"] == "white"           # подложка под прозрачный PNG
    assert got["closed"] >= 1               # ImageBitmap освобождён


def test_portrait_long_side_is_capped(result):
    assert result["portrait"] == [[1200, 1600], 1200]


def test_exif_rotated_photo_decoded_without_resize_and_drawn_upright(result):
    draw, opts = result["rotated"]
    assert draw == [1200, 1600]
    assert opts == {"imageOrientation": "from-image"}


def test_huge_photos_never_decoded_whole(result):
    opts, draw = result["huge"]
    assert (opts["resizeWidth"], opts["resizeHeight"]) == (1600, 1200) and draw == [1600, 1200]
    assert result["hugeRotated"] == [True, True]
    assert result["hugeUnknown"] == [True, True]


def test_small_image_is_not_upscaled_and_bigger_result_keeps_original(result):
    assert result["smallNotUpscaled"] == [[800, 600], {"imageOrientation": "from-image"}]
    assert result["biggerKeepsOriginal"] is True


def test_gif_and_non_images_untouched(result):
    assert result["gifUntouched"] is True
    assert result["pdfUntouched"] is True


def test_fail_soft_on_decode_error_and_on_canvas_without_jpeg(result):
    assert result["decodeFailKeepsOriginal"] is True
    assert result["noJpegKeepsOriginal"] is True


def test_decodable_heic_is_renamed_to_jpg(result):
    assert result["heic"] == ["IMG_2.jpg", "image/jpeg", [1600, 1200]]


def test_img_element_fallback_when_create_image_bitmap_throws(result):
    assert result["imgFallback"] == ["legacy.jpg", [1600, 800]]


def test_asset_cap_is_larger(result):
    assert result["asset"] == [2560, 1280]


def test_many_photos_are_decoded_one_at_a_time(result):
    assert result["serial"] == 1
    assert result["parallelNames"] == ["a.jpg", "b.jpg", "c.jpg"]


def test_hung_decoder_times_out_to_original_and_frees_queue(result):
    assert result["hangKeepsOriginal"] is True
    assert result["queueFreed"] == ["after.jpg", "image/jpeg", True]
    assert result["silentImgKeepsOriginal"] is True
    assert result["queueFreed2"] == "after2.jpg"


def test_local_preview_cache_reuses_and_revokes(result):
    assert result["localUrl"] == [True, True, None]
    assert result["replaceRevokesOld"] is True
    assert result["evicted"] == [None, True, True]
    assert result["draftKept"] is True
    assert result["forgotten"] is None
    assert result["nullSafe"] == [None, None]


def test_module_has_no_colors_literals_or_human_text():
    text = _js_without_comments(SHRINK_JS)
    assert not _HEX_OR_RGB_COLOR.findall(text)
    assert "innerHTML" not in text
    assert "import " not in text            # самостоятельный модуль, без зависимостей


# ── подключение к экранам: все места загрузки фото в Mini App ─────────────────────────────

from tests.test_miniapp_frontend import SCREENS_DIR  # noqa: E402

FORM_JS = ROOT / "miniapp" / "static" / "js" / "form.js"


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_task_cover_is_shrunk_before_size_check_and_upload_and_previewed_locally():
    text = _js_without_comments(SCREENS_DIR / "task_edit.js")
    assert 'from "../photo_shrink.js"' in text
    body = _between(text, "async function uploadCover(", "function deadlinePicker(")
    shrink = body.index("await shrinkPhoto(original)")
    assert shrink < body.index("file.size > limits.photo_max_bytes") < body.index('api("/uploads?target=task_cover"')
    assert body.index('api("/uploads?target=task_cover"') < body.index("rememberLocalPhoto(res.content, file)")
    # превью — из локального файла, сервер только если локального нет
    assert "localPhotoUrl(photoFileId) || fileUrl(photoFileId)" in text
    # убранная/заменённая обложка освобождает свой object URL
    assert text.count("forgetLocalPhoto(draft.photo_file_id)") == 2


def test_submission_photo_is_shrunk_before_size_check_and_upload():
    text = _js_without_comments(SCREENS_DIR / "submit.js")
    assert 'from "../photo_shrink.js"' in text
    body = _between(text, "async function uploadOne(", 'fileInput.addEventListener("change"')
    assert body.index("await shrinkPhoto(original)") < body.index("file.size > limits.max_bytes") < body.index('api("/uploads", {')
    assert 'form.append("file", file, file.name)' in body


@pytest.mark.parametrize("name", ["settings.js", "setup.js"])
def test_settings_photo_asset_is_shrunk_with_asset_cap(name):
    text = _js_without_comments(SCREENS_DIR / name)
    body = _between(text, "async function handleFileUpload(", 'api("/uploads?target=settings_asset"')
    assert 'item.type === "photo" ? await shrinkPhoto(original, { maxSide: MAX_SIDE_ASSET }) : original' in body
    assert body.index("shrinkPhoto(") < body.index("file.size > limits.max_bytes")


def test_dropzone_local_preview_releases_object_url():
    text = _js_without_comments(FORM_JS)
    assert "URL.revokeObjectURL(src)" in text
