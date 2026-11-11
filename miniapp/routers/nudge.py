"""Догонялка несдатчиков. Список и отправка — `services.comms.survey_nudge`.

Текст приглашения пишет менеджер в анкете. Роутер его не сочиняет.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from miniapp.deps import Principal, principal
from miniapp.outbox import enqueue
from services.comms.survey_nudge import NEED_CAP, invite_decision, screen

router = APIRouter()


class NudgeIn(BaseModel):
    poll_id: int
    confirm: bool = False


def _gate(p: Principal) -> Principal:
    if "broadcast" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})
    return p


@router.get("/app/api/forum-day/nudge")
async def nudge_list(p: Principal = Depends(principal)) -> dict:
    _gate(p)
    return await screen(p.telegram_id)


@router.post("/app/api/forum-day/nudge")
async def nudge_send(body: NudgeIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    result = await invite_decision(p.telegram_id, body.poll_id, confirm=body.confirm)
    job = result.pop("enqueue", None)
    if not job:
        return result
    queued = await enqueue("survey_nudge", job)
    if queued is None:
        return {
            "ok": False,
            "text": "Не получилось поставить напоминание в очередь. Напомните из чата с карточки анкеты.",
        }
    result["queued"] = True
    return result
