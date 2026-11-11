"""Имя делегата в PDF-шаблон сертификата.

Шаблоны рисует дизайнер (у Юлида'26 — три: участник, волонтёр, амбассадор), под имя оставлена
белая полоса. Текст шаблонов — кривые Type3, шрифта с кириллицей внутри PDF нет, поэтому имя не
набирается текстом, а рисуется Pillow шрифтом Montserrat (на нём свёрстан сам макет) и
встраивается картинкой с альфа-каналом (`/SMask`): поверх полосы видны только буквы. Картинка в
4× разрешении от размера на странице — при печати A4 буквы не мылятся.

Положение полосы — доля от размера страницы (`NAME_BOX`), снято с шаблонов Юлида'26. Шаблон с
другой вёрсткой потребует поправить только эту константу.

Сертификат компетенций: полоса под имя слева (`COMPETENCY_NAME_BOX`), справа пять шкал. В
шаблоне шкалы пустые с «0%»; бот рисует поверх синюю заливку на долю процента и белую подпись
«NN%» (`COMPETENCY_BARS`, сняты с макета 842×595 pt). 0% — заливки нет, остаётся шаблонная."""
from __future__ import annotations

import io
import json
from pathlib import Path

_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_FONT = "Montserrat-ExtraBold.ttf"

# (центр x, центр y от ВЕРХА, макс. ширина, высота) — доли ширины/высоты страницы.
NAME_BOX = (0.5, 0.452, 0.76, 0.075)
COMPETENCY_NAME_BOX = (0.304, 0.4504, 0.45, 0.075)
NAME_COLOR = (3, 126, 243)  # синий АЙСЕК — как плашки и подписи макета
_SUPERSAMPLE = 4

# Шкалы компетенций в pt макета 842×595: левый край, правый край, верх каждой шкалы (сверху
# вниз, порядок подписей шаблона), высота, отступ подписи от левого края, кегль подписи.
COMPETENCY_BARS = {"page": (842.0, 595.0), "x0": 510.0, "x1": 794.0,
                   "tops": (156.0, 211.0, 267.0, 326.0, 383.0), "h": 35.0, "pad": 19.0, "size": 15.0}
_BAR_FONT = "Montserrat-Bold.ttf"


def _box(item) -> dict | None:
    """Доли левого верхнего угла и размера. Число вне листа — рамка не годится."""
    if not isinstance(item, dict):
        return None
    try:
        x, y, w, h = (float(item[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(0 <= value <= 1 for value in (x, y, w, h)):
        return None
    if w <= 0 or h <= 0 or x + w > 1.001 or y + h > 1.001:
        return None
    box = {"x": x, "y": y, "w": w, "h": h}
    comp = item.get("competency")
    if isinstance(comp, str) and comp.strip():
        box["competency"] = comp.strip()
    return box


def parse_markup(raw: str | None) -> dict | None:
    """JSON долей рядом с файлом. Пусто и мусор — нет долей, рендер берёт константы."""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict = {}
    for key in ("name", "date", "city"):
        box = _box(data.get(key))
        if box:
            out[key] = box
    scales = []
    for item in data.get("scales") or []:
        box = _box(item)
        if box:
            scales.append(box)
    if scales:
        out["scales"] = scales
    return out or None


def markup_error(data) -> str | None:
    """Пустая строка ошибки, если доли можно сохранить. Текст говорит, что поправить."""
    if not isinstance(data, dict):
        return "Не получилось прочитать рамки. Поставьте их на странице ещё раз и сохраните."
    for key, title in (("name", "Имя"), ("date", "Дата"), ("city", "Город")):
        if key in data and data[key] is not None and _box(data[key]) is None:
            return f"Рамка «{title}» вышла за страницу. Поставьте её целиком на лист."
    scales = data.get("scales") or []
    if not isinstance(scales, list):
        return "Шкалы не прочитались. Поставьте их ещё раз и сохраните."
    for index, item in enumerate(scales, start=1):
        if _box(item) is None:
            return f"Рамка «Шкала {index}» вышла за страницу. Поставьте её целиком на лист."
    return None


def dump_markup(data: dict) -> str:
    clean: dict = {}
    for key in ("name", "date", "city"):
        box = _box(data.get(key))
        if box:
            clean[key] = {k: box[k] for k in ("x", "y", "w", "h")}
    scales = []
    for item in data.get("scales") or []:
        box = _box(item)
        if not box:
            continue
        row = {k: box[k] for k in ("x", "y", "w", "h")}
        if box.get("competency"):
            row["competency"] = box["competency"]
        scales.append(row)
    if scales:
        clean["scales"] = scales
    return json.dumps(clean, ensure_ascii=False)


def _name_image(name: str, box_w_pt: float, box_h_pt: float):
    """RGBA-картинка имени, вписанная в полосу: кегль уменьшается, пока строка не влезет."""
    from PIL import Image, ImageDraw, ImageFont

    w_px, h_px = int(box_w_pt * _SUPERSAMPLE), int(box_h_pt * _SUPERSAMPLE)
    size = int(h_px * 0.62)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    while True:
        font = ImageFont.truetype(str(_FONTS / _FONT), size)
        if probe.textlength(name, font=font) <= w_px or size <= 8 * _SUPERSAMPLE:
            break
        size -= _SUPERSAMPLE
    img = Image.new("RGBA", (w_px, h_px), NAME_COLOR + (0,))
    ImageDraw.Draw(img).text((w_px / 2, h_px / 2), name, font=font, fill=NAME_COLOR + (255,), anchor="mm")
    return img


def _bars_image(percents, scale: float):
    """RGBA-картинка колонки шкал: на каждую шкалу с процентом > 0 — синяя пилюля на долю
    ширины (не уже подписи) и белое «NN%». Возвращает (картинка, x, верх, ширина, высота) в pt
    макета."""
    from PIL import Image, ImageDraw, ImageFont

    b = COMPETENCY_BARS
    top, bottom = b["tops"][0], b["tops"][-1] + b["h"]
    width, height = b["x1"] - b["x0"], bottom - top
    k = scale * _SUPERSAMPLE
    img = Image.new("RGBA", (round(width * k), round(height * k)), NAME_COLOR + (0,))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(_FONTS / _BAR_FONT), round(b["size"] * k))
    for bar_top, pct in zip(b["tops"], percents):
        if not pct or pct <= 0:
            continue
        pct = min(int(pct), 100)
        label = f"{pct}%"
        y0, h = (bar_top - top) * k, b["h"] * k
        bar_w = max(width * k * pct / 100, draw.textlength(label, font=font) + 2 * b["pad"] * k)
        draw.rounded_rectangle((0, y0, min(bar_w, width * k) - 1, y0 + h - 1), radius=h / 2,
                               fill=NAME_COLOR + (255,))
        draw.text((b["pad"] * k, y0 + h / 2), label, font=font, fill=(255, 255, 255, 255), anchor="lm")
    return img, b["x0"], top, width, height


def _one_bar(pct: int, box_w_pt: float, box_h_pt: float):
    """Одна шкала в своей рамке. 0% — картинки нет, на листе остаётся ноль шаблона."""
    from PIL import Image, ImageDraw, ImageFont

    pct = min(int(pct), 100)
    if pct <= 0:
        return None
    k = _SUPERSAMPLE
    w_px, h_px = max(1, int(box_w_pt * k)), max(1, int(box_h_pt * k))
    img = Image.new("RGBA", (w_px, h_px), NAME_COLOR + (0,))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(_FONTS / _BAR_FONT), max(8, int(h_px * 0.45)))
    label = f"{pct}%"
    pad = h_px * 0.15
    bar_w = max(w_px * pct / 100, draw.textlength(label, font=font) + 2 * pad)
    draw.rounded_rectangle((0, 0, min(bar_w, w_px) - 1, h_px - 1), radius=h_px / 2,
                           fill=NAME_COLOR + (255,))
    draw.text((pad, h_px / 2), label, font=font, fill=(255, 255, 255, 255), anchor="lm")
    return img


def _frac_rect(left, top, page_w, page_h, box: dict):
    w, h = page_w * box["w"], page_h * box["h"]
    x = left + page_w * box["x"]
    y = top - page_h * box["y"] - h
    return x, y, w, h


def _image_xobject(writer, img):
    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

    def stream(data: bytes, extra: dict):
        obj = DecodedStreamObject()
        obj.set_data(data)
        obj = obj.flate_encode()
        obj.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(img.width),
            NameObject("/Height"): NumberObject(img.height),
            NameObject("/BitsPerComponent"): NumberObject(8),
            **extra,
        })
        return writer._add_object(obj)

    smask = stream(img.getchannel("A").tobytes(), {NameObject("/ColorSpace"): NameObject("/DeviceGray")})
    return stream(img.convert("RGB").tobytes(), {
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/SMask"): smask,
    })


def render_certificate(template_pdf: bytes, name: str, name_box: tuple = NAME_BOX,
                       percents: list[int] | None = None, markup: dict | None = None,
                       date_text: str = "", city_text: str = "") -> bytes:
    """PDF первой страницы шаблона с вписанным именем (и шкалами компетенций, если переданы
    `percents` — по порядку шкал шаблона). `markup` — доли рамок этого файла. Нет долей —
    прежние `NAME_BOX` и `COMPETENCY_BARS`. Синхронный (Pillow + pypdf) — вызывающий зовёт
    через `asyncio.to_thread`. Именной слой `{name}` не ищется и не создаётся."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    reader = PdfReader(io.BytesIO(template_pdf))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    # Альбомный лист часто приходит книжным MediaBox с /Rotate 90: поворот переносим в
    # содержимое, иначе доли NAME_BOX считались бы от неповёрнутой страницы и имя легло бы боком.
    if int(page.get("/Rotate", 0) or 0) % 360:
        page.transfer_rotation_to_content()
    # Доли — от видимой области (CropBox; без него pypdf отдаёт MediaBox).
    left, bottom, right, top = (float(v) for v in page.cropbox)
    page_w, page_h = right - left, top - bottom

    # (имя XObject, картинка, x, y низа, ширина, высота) в pt страницы
    images = []
    name_frac = (markup or {}).get("name")
    if name_frac:
        x, y, box_w, box_h = _frac_rect(left, top, page_w, page_h, name_frac)
    else:
        cx, cy_top, wf, hf = name_box
        box_w, box_h = page_w * wf, page_h * hf
        x = left + page_w * cx - box_w / 2
        y = top - page_h * cy_top - box_h / 2
    images.append(("/CertName", _name_image(name.strip(), box_w, box_h), x, y, box_w, box_h))
    for key, text, label in (
        ("date", date_text, "/CertDate"),
        ("city", city_text, "/CertCity"),
    ):
        box = (markup or {}).get(key)
        if not box or not str(text or "").strip():
            continue
        rx, ry, rw, rh = _frac_rect(left, top, page_w, page_h, box)
        images.append((label, _name_image(str(text).strip(), rw, rh), rx, ry, rw, rh))
    scales = (markup or {}).get("scales")
    if scales and percents:
        for index, box in enumerate(scales):
            pct = percents[index] if index < len(percents) else 0
            rx, ry, rw, rh = _frac_rect(left, top, page_w, page_h, box)
            img = _one_bar(pct, rw, rh)
            if img is not None:
                images.append((f"/CertBar{index}", img, rx, ry, rw, rh))
    elif percents:
        sx = page_w / COMPETENCY_BARS["page"][0]
        sy = page_h / COMPETENCY_BARS["page"][1]
        img, bx, btop, bw, bh = _bars_image(percents, sx)
        images.append(("/CertBars", img, left + bx * sx, top - (btop + bh) * sy, bw * sx, bh * sy))

    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else DictionaryObject()
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if xobjects is not None else DictionaryObject()
    for key, img, *_ in images:
        xobjects[NameObject(key)] = _image_xobject(writer, img)
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources

    def ops(text: str):
        obj = DecodedStreamObject()
        obj.set_data(text.encode("ascii"))
        return writer._add_object(obj)

    draw = "".join(f"q {w:.2f} 0 0 {h:.2f} {ix:.2f} {iy:.2f} cm {key} Do Q\n" for key, _, ix, iy, w, h in images)
    contents = page.get("/Contents")
    old = contents.get_object() if contents is not None else None
    parts = list(old) if isinstance(old, ArrayObject) else ([contents] if contents is not None else [])
    page[NameObject("/Contents")] = ArrayObject([ops("q\n"), *parts, ops(f"\nQ\n{draw}")])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
