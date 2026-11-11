"""Снять отказ с одной заявки. Флип — `services.applications.approve_rejected`.

Роутер не одобряет список и письмо сам не шлёт: решение ставится в очередь бота.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from miniapp.deps import Principal, principal
from miniapp.outbox import enqueue
from services.applications.approve_rejected import NEED_CAP, approve_decision, rejected_screen

router = APIRouter()


class LiftIn(BaseModel):
    telegram_id: int
    confirm: bool = False


def _gate(p: Principal) -> Principal:
    if "moderate_reg" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})
    return p


@router.get("/app/api/forum-day/lift-reject")
async def lift_reject_list(q: str = "", p: Principal = Depends(principal)) -> dict:
    _gate(p)
    return await rejected_screen(p.telegram_id, q)


@router.post("/app/api/forum-day/lift-reject")
async def lift_reject_send(body: LiftIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    result = await approve_decision(p.telegram_id, body.telegram_id, confirm=body.confirm)
    job = result.pop("enqueue", None)
    if not job:
        return result
    queued = await enqueue("application_decided", job)
    if queued is None:
        return {
            "ok": False,
            "text": (
                "Отказ снят, но письмо с решением не встало в очередь. "
                "Напишите человеку из чата, что заявка одобрена."
            ),
        }
    result["queued"] = True
    return result
