"""Экран «Позвать резерв».

Отбор — `services.forum.reserve_invite`, тот же, что кнопка чата в
`handlers/forum/admin_reserve.py`. Условия списка здесь не повторяются.
Письмо ставится в очередь бота и уходит через `deliver`: строка приглашения
пишется до отправки. Файл к письму остаётся в чате.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from miniapp.deps import Principal, principal
from miniapp.outbox import enqueue
from services.forum.reserve_invite import NEED_CAP, invite_decision, screen

router = APIRouter()


class InviteIn(BaseModel):
    ids: list[int] = Field(default_factory=list)
    mode: str = "selected"
    confirm: bool = False


def _gate(p: Principal) -> Principal:
    if "moderate_reg" not in p.caps:
        raise HTTPException(403, {"reason": "no_cap", "text": NEED_CAP})
    return p


@router.get("/app/api/forum-day/reserve")
async def reserve_list(p: Principal = Depends(principal)) -> dict:
    _gate(p)
    return await screen(p.telegram_id)


@router.post("/app/api/forum-day/reserve")
async def reserve_invite(body: InviteIn, p: Principal = Depends(principal)) -> dict:
    _gate(p)
    result = await invite_decision(
        p.telegram_id, mode=body.mode, selected=body.ids, confirm=body.confirm,
    )
    job = result.pop("enqueue", None)
    if not job:
        return result
    queued = await enqueue("reserve_invite", job)
    if queued is None:
        return {
            "ok": False,
            "text": (
                "Не получилось поставить письмо в очередь. "
                "Позовите из чата кнопкой «Позвать резерв»."
            ),
        }
    result["queued"] = True
    return result
