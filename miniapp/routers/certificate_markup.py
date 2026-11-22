"""Разметка сертификата: превью первой страницы и доли рамок.

Экран живёт в приложении: дашборд базу не пишет. Пункта разметки в чате нет.
Шаблон — обычный PDF, именные слои не нужны. Шкала к компетенции — кнопка.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from database import certificates_db as cdb
from database import session_enroll_db
from domain.cities import default_city_code
from miniapp.deps import Principal, principal
from miniapp import telegram_api
from miniapp.telegram_api import TelegramApiError
from services.forum.certificate_render import (
    dump_markup,
    markup_error,
    page_aspect,
    parse_markup,
    pdf_error,
    preview_pdf,
)

router = APIRouter()

NEED_CAP = "Чтобы размечать сертификат, нужно право заявок."
NO_TEMPLATE = "Сначала загрузите PDF шаблона."
BAD_SLOT = "Не понял, какой шаблон. Выберите строку из списка."
SAVE_FAIL = "Не получилось сохранить PDF. Пришлите его в чат, в шаблоны сертификата."
_MAX_PDF = 20 * 1024 * 1024


def _gate(p: Principal) -> None:
    if "moderate_reg" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})


async def _known(kind: str, variant: str) -> bool:
    if kind in cdb.KINDS:
        ok = True
    else:
        ok = await cdb.get_kind(kind) is not None
    if not ok:
        return False
    if variant == cdb.VARIANT_ALL:
        return True
    return len(variant) == 10 and variant[4] == "-" and variant[7] == "-"


async def _slots() -> list[dict]:
    rows = {(row["kind"], row["variant"]): row for row in await cdb.list_template_rows()}
    keys = [(kind, cdb.VARIANT_ALL) for kind in cdb.KINDS]
    titles = {kind: cdb.KIND_SHORT[kind] for kind in cdb.KINDS}
    for kind_row in await cdb.list_kinds():
        titles[kind_row["code"]] = kind_row["title"]
        key = (kind_row["code"], cdb.VARIANT_ALL)
        if key not in keys:
            keys.append(key)
    for key in rows:
        if key not in keys:
            keys.append(key)
    out = []
    for kind, variant in keys:
        row = rows.get((kind, variant))
        label = titles.get(kind) or cdb.KIND_SHORT.get(kind) or "Сертификат"
        out.append({
            "kind": kind,
            "variant": variant,
            "title": f"{label} · {cdb.variant_label(variant)}",
            "has_file": bool(row),
            "markup": parse_markup(row["markup"]) if row else None,
        })
    return out


class MarkupIn(BaseModel):
    kind: str
    variant: str
    markup: dict


@router.get("/app/api/certificates/markup")
async def markup_screen(p: Principal = Depends(principal)) -> dict:
    _gate(p)
    names = [row["name"] for row in await session_enroll_db.list_competencies(default_city_code())]
    return {"templates": await _slots(), "competencies": [name for name in names if name]}


@router.get("/app/api/certificates/markup/preview")
async def markup_preview(
    request: Request,
    kind: str = Query(""),
    variant: str = Query(""),
    p: Principal = Depends(principal),
) -> Response:
    _gate(p)
    if not await _known(kind, variant):
        raise HTTPException(400, {"reason": "bad_template", "text": BAD_SLOT})
    row = next((item for item in await cdb.list_template_rows()
                if item["kind"] == kind and item["variant"] == variant), None)
    if row is None:
        raise HTTPException(404, {"reason": "no_template", "text": NO_TEMPLATE})
    cfg = request.app.state.cfg
    try:
        info = await telegram_api.get_file(cfg, row["file_id"])
        file_path = info.get("file_path")
        if not file_path:
            raise TelegramApiError("not_found")
        stream = await telegram_api.download_file(cfg, file_path)
        chunks = []
        async for chunk in stream.chunks():
            chunks.append(chunk)
            if sum(len(part) for part in chunks) > _MAX_PDF:
                break
        raw = b"".join(chunks)
    except TelegramApiError:
        raise HTTPException(404, {"reason": "no_template", "text": "Не получилось открыть PDF. Загрузите файл ещё раз."})
    if len(raw) > _MAX_PDF:
        raise HTTPException(400, {"reason": "too_large", "text": "Файл больше 20 МБ. Пришлите PDF поменьше."})
    if pdf_error(raw):
        raise HTTPException(404, {"reason": "not_pdf", "text": "Не получилось открыть этот PDF. Пересохраните его и пришлите ещё раз."})
    shown = preview_pdf(raw)
    return Response(shown, media_type="application/pdf", headers={
        "X-Page-Aspect": f"{page_aspect(shown):.4f}",
        "Cache-Control": "no-store",
    })


@router.post("/app/api/certificates/markup")
async def markup_save(body: MarkupIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    if not await _known(body.kind, body.variant):
        raise HTTPException(400, {"reason": "bad_template", "text": BAD_SLOT})
    error = markup_error(body.markup)
    if error:
        raise HTTPException(400, {"reason": "bad_markup", "text": error})
    saved = await cdb.set_markup(body.kind, body.variant, dump_markup(body.markup))
    if not saved:
        raise HTTPException(400, {"reason": "no_template", "text": NO_TEMPLATE})
    return {"ok": True, "text": "Рамки сохранены."}


@router.post("/app/api/certificates/markup/file")
async def markup_file(
    request: Request,
    kind: str = Query(""),
    variant: str = Query(""),
    file: UploadFile = File(...),
    p: Principal = Depends(principal),
) -> dict:
    _gate(p)
    if not await _known(kind, variant):
        raise HTTPException(400, {"reason": "bad_template", "text": BAD_SLOT})
    raw = await file.read(_MAX_PDF + 1)
    if len(raw) > _MAX_PDF:
        raise HTTPException(400, {"reason": "too_large", "text": "Файл больше 20 МБ. Пришлите PDF поменьше."})
    error = pdf_error(raw)
    if error:
        raise HTTPException(400, {"reason": "not_pdf", "text": error})
    cfg = request.app.state.cfg
    try:
        result = await telegram_api.send_document(
            cfg, p.telegram_id, raw, "template.pdf", "application/pdf",
            caption="Шаблон сертификата сохранён. Рамки ставятся на этой странице.",
        )
    except TelegramApiError:
        raise HTTPException(502, {"reason": "telegram", "text": SAVE_FAIL})
    file_id = (result.get("document") or {}).get("file_id")
    if not file_id:
        raise HTTPException(502, {"reason": "telegram", "text": SAVE_FAIL})
    cleared = await cdb.set_template(kind, variant, file_id, p.telegram_id)
    text = "PDF сохранён."
    if cleared:
        text += " Рамки с прошлого файла сняты. Поставьте их заново."
    return {"ok": True, "text": text}
