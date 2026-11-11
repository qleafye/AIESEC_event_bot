"""Перенос сканов с тренировки. Список и запись — `services.forum.checkin_training_move`.

Роутер сессию не выдумывает: менеджер жмёт кнопку с залом и временем.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from miniapp.deps import Principal, principal
from services.forum.checkin_training_move import NEED_CAP, app_screen, move_decision

router = APIRouter()


class MoveIn(BaseModel):
    session_id: int
    confirm: bool = False


def _gate(p: Principal) -> Principal:
    if "moderate_reg" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})
    return p


@router.get("/app/api/forum-day/training-move")
async def training_move_list(p: Principal = Depends(principal)) -> dict:
    _gate(p)
    return await app_screen(p.telegram_id)


@router.post("/app/api/forum-day/training-move")
async def training_move_send(body: MoveIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    return await move_decision(p.telegram_id, body.session_id, confirm=body.confirm)
