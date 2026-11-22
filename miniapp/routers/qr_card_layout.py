"""Рамки на макете карточки QR. Файл кладёт менеджер в боте, доли — здесь.

Превью — исходная картинка, не PDF. Право то же, что у кнопок `qrct_*`: настройки.
"""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from database.db import qr_card_layout_get, qr_card_layout_put
from miniapp.deps import Principal, principal
from miniapp import telegram_api
from miniapp.telegram_api import TelegramApiError
from services.forum.qr_card import layout_markup

router = APIRouter()

NEED_CAP = "Чтобы двигать код на макете, нужно право настроек"
NO_FILE = "Сначала загрузите картинку макета в боте"
BAD_FRAME = "Поставьте рамку кода целиком на картинку."
BAD_IMAGE = "Не получилось открыть картинку макета. Загрузите PNG или JPEG ещё раз."
_MAX_IMAGE = 10 * 1024 * 1024


def _gate(p: Principal) -> None:
    if "settings" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})


class MarkupIn(BaseModel):
    markup: dict


def _aspect(raw: bytes) -> tuple[str, float]:
    if raw.startswith(b"%PDF"):
        raise HTTPException(404, {"reason": "not_image", "text": BAD_IMAGE})
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img.load()
        width, height = img.size
        if width < 1 or height < 1:
            raise ValueError("пустая картинка")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, {"reason": "not_image", "text": BAD_IMAGE})
    media = "image/jpeg" if raw.startswith(b"\xff\xd8") else "image/png"
    return media, width / height


@router.get("/app/api/qr-card-layout")
async def layout_screen(p: Principal = Depends(principal)) -> dict:
    _gate(p)
    row = await qr_card_layout_get()
    if row is None:
        return {"has_file": False, "markup": None}
    return {"has_file": True, "markup": row["markup"]}


@router.get("/app/api/qr-card-layout/preview")
async def layout_preview(request: Request, p: Principal = Depends(principal)) -> Response:
    _gate(p)
    row = await qr_card_layout_get()
    if row is None:
        raise HTTPException(404, {"reason": "no_file", "text": NO_FILE})
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
            if sum(len(part) for part in chunks) > _MAX_IMAGE:
                break
        raw = b"".join(chunks)
    except TelegramApiError:
        raise HTTPException(404, {"reason": "no_file", "text": BAD_IMAGE})
    if len(raw) > _MAX_IMAGE or raw.startswith(b"%PDF"):
        raise HTTPException(404, {"reason": "not_image", "text": BAD_IMAGE})
    media, aspect = _aspect(raw)
    return Response(raw, media_type=media, headers={
        "X-Page-Aspect": f"{aspect:.4f}",
        "Cache-Control": "no-store",
    })


@router.post("/app/api/qr-card-layout")
async def layout_save(body: MarkupIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    cleaned = layout_markup(body.markup)
    if cleaned is None:
        raise HTTPException(400, {"reason": "bad_markup", "text": BAD_FRAME})
    row = await qr_card_layout_get()
    if row is None:
        raise HTTPException(404, {"reason": "no_file", "text": NO_FILE})
    await qr_card_layout_put(row["file_id"], cleaned)
    return {"ok": True, "text": "Рамки сохранены."}
