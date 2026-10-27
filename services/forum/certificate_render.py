"""Имя делегата в PDF-шаблон сертификата.

Шаблоны рисует дизайнер (у Юлида'26 — три: участник, волонтёр, амбассадор), под имя оставлена
белая полоса. Текст шаблонов — кривые Type3, шрифта с кириллицей внутри PDF нет, поэтому имя не
набирается текстом, а рисуется Pillow шрифтом Montserrat (на нём свёрстан сам макет) и
встраивается картинкой с альфа-каналом (`/SMask`): поверх полосы видны только буквы. Картинка в
4× разрешении от размера на странице — при печати A4 буквы не мылятся.

Положение полосы — доля от размера страницы (`NAME_BOX`), снято с шаблонов Юлида'26. Шаблон с
другой вёрсткой потребует поправить только эту константу."""
from __future__ import annotations

import io
from pathlib import Path

_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_FONT = "Montserrat-ExtraBold.ttf"

# (центр x, центр y от ВЕРХА, макс. ширина, высота) — доли ширины/высоты страницы.
NAME_BOX = (0.5, 0.452, 0.76, 0.075)
NAME_COLOR = (3, 126, 243)  # синий АЙСЕК — как плашки и подписи макета
_SUPERSAMPLE = 4


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


def render_certificate(template_pdf: bytes, name: str) -> bytes:
    """PDF первой страницы шаблона с вписанным именем. Синхронный (Pillow + pypdf) — вызывающий
    зовёт через `asyncio.to_thread`."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    reader = PdfReader(io.BytesIO(template_pdf))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    left, bottom, right, top = (float(v) for v in page.mediabox)
    page_w, page_h = right - left, top - bottom

    cx, cy_top, wf, hf = NAME_BOX
    box_w, box_h = page_w * wf, page_h * hf
    x = left + page_w * cx - box_w / 2
    y = top - page_h * cy_top - box_h / 2

    img_ref = _image_xobject(writer, _name_image(name.strip(), box_w, box_h))

    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else DictionaryObject()
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if xobjects is not None else DictionaryObject()
    xobjects[NameObject("/CertName")] = img_ref
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources

    def ops(text: str):
        obj = DecodedStreamObject()
        obj.set_data(text.encode("ascii"))
        return writer._add_object(obj)

    contents = page.get("/Contents")
    old = contents.get_object() if contents is not None else None
    parts = list(old) if isinstance(old, ArrayObject) else ([contents] if contents is not None else [])
    page[NameObject("/Contents")] = ArrayObject([
        ops("q\n"), *parts,
        ops(f"\nQ\nq {box_w:.2f} 0 0 {box_h:.2f} {x:.2f} {y:.2f} cm /CertName Do Q\n"),
    ])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

