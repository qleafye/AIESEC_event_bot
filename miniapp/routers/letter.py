"""Письмо одному человеку. Допуск и текст — `services.applications.personal_letter`.

Роутер письмо не сочиняет и в рассылку его не превращает.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from miniapp.deps import Principal, principal
from miniapp.outbox import enqueue
from services.applications.personal_letter import NEED_CAP, letter_decision, screen

router = APIRouter()


class LetterIn(BaseModel):
    telegram_id: int
    text: str = ""
    confirm: bool = False


def _gate(p: Principal) -> Principal:
    if "moderate_reg" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})
    return p


@router.get("/app/api/forum-day/letter")
async def letter_list(q: str = "", p: Principal = Depends(principal)) -> dict:
    _gate(p)
    return await screen(p.telegram_id, q)


@router.post("/app/api/forum-day/letter")
async def letter_send(body: LetterIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    result = await letter_decision(
        p.telegram_id, body.telegram_id, body.text, confirm=body.confirm,
    )
    job = result.pop("enqueue", None)
    if not job:
        return result
    queued = await enqueue("personal_letter", job)
    if queued is None:
        return {
            "ok": False,
            "text": "Не получилось поставить письмо в очередь. Напишите из чата кнопкой «Написать».",
        }
    result["queued"] = True
    return result
