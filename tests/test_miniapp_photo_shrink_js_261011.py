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
const DIMS = {};            // имя файла -> [w, h] у фейкового декодера
let OUT_SIZE = 300000;      // размер JPEG, который «выдаёт» canvas
let OUT_TYPE = "image/jpeg";
const log = { opts: [], draws: [], fills: [], quality: [], closed: 0, cibCalls: 0, active: 0, maxActive: 0, revoked: [] };

globalThis.createImageBitmap = async (file, opts) => {
  log.cibCalls += 1;
  log.opts.push(opts);
  log.active += 1;
  log.maxActive = Math.max(log.maxActive, log.active);
  await new Promise((ok) => setTimeout(ok, 5));
  log.active -= 1;
  if (!DIMS[file.name]) throw new Error("cannot decode");
  const [width, height] = DIMS[file.name];
  return { width, height, close() { log.closed += 1; } };
};

globalThis.document = {
  createElement(tag) {
    const canvas = {
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
    return canvas;
  },
};

const realRevoke = URL.revokeObjectURL.bind(URL);
URL.revokeObjectURL = (u) => { log.revoked.push(u); realRevoke(u); };

const m = await import(%(url)s);
const file = (name, size, type) => new File([new Uint8Array(size)], name, { type });
const r = {};

// 1. альбомное фото с камеры 4000×3000, 1,4 МБ -> JPEG 1600×1200
DIMS["IMG_0001.JPG"] = [4000, 3000];
{
  const out = await m.shrinkPhoto(file("IMG_0001.JPG", 1435564, "image/jpeg"));
  r.landscape = { name: out.name, type: out.type, size: out.size, draw: log.draws.at(-1),
    opts: log.opts.at(-1), quality: log.quality.at(-1), fill: log.fills.at(-1), closed: log.closed };
}
// 2. вертикальное 3000×4000 -> 1200×1600
DIMS["portrait.jpeg"] = [3000, 4000];
{ await m.shrinkPhoto(file("portrait.jpeg", 2000000, "image/jpeg")); r.portrait = log.draws.at(-1); }
// 3. маленький PNG, JPEG вышел больше -> оригинал
DIMS["shot.png"] = [800, 600];
{
  OUT_SIZE = 90000;
  const f = file("shot.png", 50000, "image/png");
  const out = await m.shrinkPhoto(f);
  r.biggerKeepsOriginal = out === f;
  r.smallNotUpscaled = log.draws.at(-1);
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
// 6. декодер не справился, <img> нет -> оригинал
{ const f = file("broken.heic", 3000000, "image/heic"); r.decodeFailKeepsOriginal = (await m.shrinkPhoto(f)) === f; }
// 7. HEIC, который движок декодирует -> .jpg
DIMS["IMG_2.HEIC"] = [4032, 3024];
{ const out = await m.shrinkPhoto(file("IMG_2.HEIC", 2500000, "image/heic")); r.heic = [out.name, out.type]; }
// 8. canvas не умеет JPEG (вернул PNG) -> оригинал
DIMS["nojpeg.jpg"] = [4000, 3000];
{
  OUT_TYPE = "image/png";
  const f = file("nojpeg.jpg", 2000000, "image/jpeg");
  r.noJpegKeepsOriginal = (await m.shrinkPhoto(f)) === f;
  OUT_TYPE = "image/jpeg";
}
// 9. фолбэк на <img>, когда createImageBitmap бросает (старый движок)
{
  globalThis.Image = class {
    set src(u) { this.naturalWidth = 2000; this.naturalHeight = 1000; setTimeout(() => this.onload(), 0); }
  };
  const out = await m.shrinkPhoto(file("legacy.jpg", 2000000, "image/jpeg"));
  r.imgFallback = [out.name, log.draws.at(-1)];
  delete globalThis.Image;
}
// 10. ассет оформления — потолок 2560
DIMS["poster.jpg"] = [5000, 2500];
{ await m.shrinkPhoto(file("poster.jpg", 4000000, "image/jpeg"), { maxSide: m.MAX_SIDE_ASSET }); r.asset = log.draws.at(-1); }
// 11. три фото разом — декодируются по одному
DIMS["a.jpg"] = [4000, 3000]; DIMS["b.jpg"] = [4000, 3000]; DIMS["c.jpg"] = [4000, 3000];
{
  log.maxActive = 0;
  const outs = await Promise.all(["a.jpg", "b.jpg", "c.jpg"].map((n) => m.shrinkPhoto(file(n, 2000000, "image/jpeg"))));
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
  for (let i = 0; i < 6; i += 1) m.rememberLocalPhoto(`F${i}`, shrunk);
  r.evicted = [m.localPhotoUrl("FILE_A"), log.revoked.includes(replaced), m.localPhotoUrl("F5") !== null];
  m.forgetLocalPhoto("F5");
  r.forgotten = m.localPhotoUrl("F5");
  r.nullSafe = [m.rememberLocalPhoto(null, shrunk), m.localPhotoUrl(null)];
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
    assert got["opts"] == {"imageOrientation": "from-image"}
    assert got["quality"] == ["image/jpeg", 0.82]
    assert got["fill"] == "white"           # подложка под прозрачный PNG
    assert got["closed"] >= 1               # ImageBitmap освобождён


def test_portrait_long_side_is_capped(result):
    assert result["portrait"] == [1200, 1600]


def test_small_image_is_not_upscaled_and_bigger_result_keeps_original(result):
    assert result["smallNotUpscaled"] == [800, 600]
    assert result["biggerKeepsOriginal"] is True


def test_gif_and_non_images_untouched(result):
    assert result["gifUntouched"] is True
    assert result["pdfUntouched"] is True


def test_fail_soft_on_decode_error_and_on_canvas_without_jpeg(result):
    assert result["decodeFailKeepsOriginal"] is True
    assert result["noJpegKeepsOriginal"] is True


def test_decodable_heic_is_renamed_to_jpg(result):
    assert result["heic"] == ["IMG_2.jpg", "image/jpeg"]


def test_img_element_fallback_when_create_image_bitmap_throws(result):
    assert result["imgFallback"] == ["legacy.jpg", [1600, 800]]


def test_asset_cap_is_larger(result):
    assert result["asset"] == [2560, 1280]


def test_many_photos_are_decoded_one_at_a_time(result):
    assert result["serial"] == 1
    assert result["parallelNames"] == ["a.jpg", "b.jpg", "c.jpg"]


def test_local_preview_cache_reuses_and_revokes(result):
    assert result["localUrl"] == [True, True, None]
    assert result["replaceRevokesOld"] is True
    assert result["evicted"] == [None, True, True]
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
