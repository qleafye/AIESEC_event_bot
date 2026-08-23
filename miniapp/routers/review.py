"""Phase 19 (D-04 волна 2, D-07, D-09): очередь проверки сдач из Mini App.

Единственное место фазы, где веб-процесс НАЧИСЛЯЕТ монеты. Порядок решения — строго как у
`handlers/admin_gamification.py::grev_approve` (импортировать нельзя — aiogram):

    проверка скоупа -> claim_submission(...) -> ТОЛЬКО при won:
        add_coins(source="task") -> outbox submission_reviewed -> сообщение делегату

`claim_submission` — атомарный `UPDATE … WHERE status = 'pending'`, True ровно у одного
вызова (T-19-27). `add_coins` дописывает строку в журнал и сам по себе НЕ идемпотентен,
поэтому двойной тап / второй менеджер получают `{ok: false, reason: "already"}` без
монет и без сообщения. Отклонение `add_coins` не вызывает никогда.

Очередь — по одной карточке (D-07): `GET /review/next?offset=N`; «⏭ Пропустить» — чисто
клиентский `offset+1`, на сервере ничего не меняется (как `grev_skip` пишет только в FSM).

Городской скоуп (T-19-28) — зеркало `handlers/admin_core.py::_submission_out_of_scope`:
сравнивается город ДЕЛЕГАТА (не задания) с привязкой менеджера `Principal.city`; модуль
городов выключен или привязки нет -> без фильтра.

Сбой отправки сообщения делегату начисление НЕ откатывает — только лог (паритет с ботом).
Sheets пересобирает бот по событию outbox (план 19-08, T-19-34).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from cities import cities_module_on, city_label, city_scope, normalize_city
from database.db import (
    add_coins,
    claim_submission,
    count_rejected_submissions,
    get_pending_submissions,
    get_pending_submissions_count,
    get_submission,
    get_submission_parts_or_legacy,
    get_task,
    get_user,
    task_title,
)
from game_labels import category_label, proof_types_label
from settings_schema import get_setting_typed

from miniapp import telegram_api
from miniapp.deps import Principal, require_cap, require_section
from miniapp.outbox import enqueue
from miniapp.telegram_api import TelegramApiError

logger = logging.getLogger(__name__)
router = APIRouter()

# T-19-30: сумма из тела запроса — целое в разумных границах; дефолт — из задания.
COINS_MIN = 1
COINS_MAX = 100_000
REASON_MAX = 500

# Те же слова, что алерт бота (_SUBMISSION_OUT_OF_SCOPE_ALERT) — менеджер видел их в боте.
OUT_OF_SCOPE_TEXT = "Эта сдача из другого города — переключите город."
REASON_REQUIRED_TEXT = "Напишите причину отклонения — делегат увидит её в сообщении."
BAD_COINS_TEXT = f"Сумма — целое число от {COINS_MIN} до {COINS_MAX}, например 15."


# ── скоуп ────────────────────────────────────────────────────────────────────────────────

async def manager_scope(p: Principal):
    """`city_scope` для SQL-очереди: модуль выключен или привязки нет -> None (всё)."""
    if not await cities_module_on() or p.city is None:
        return None
    return city_scope(normalize_city(p.city))


async def submission_out_of_scope(p: Principal, user_id: int) -> bool:
    """Зеркало `_submission_out_of_scope`: город ДЕЛЕГАТА vs привязка менеджера."""
    if not await cities_module_on() or p.city is None:
        return False
    user = await get_user(user_id)
    return normalize_city((user or {}).get("event_city")) != normalize_city(p.city)


async def _load_for_decision(p: Principal, sid: int) -> tuple[dict, dict]:
    submission = await get_submission(sid)
    task = await get_task(submission["task_id"]) if submission is not None else None
    if submission is None or task is None:
        raise HTTPException(404, {"reason": "not_found"})
    if await submission_out_of_scope(p, submission["user_id"]):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    return submission, task


# ── GET /app/api/review/next ─────────────────────────────────────────────────────────────

def _parse_offset(raw) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


@router.get("/app/api/review/next")
async def review_next(
    offset: str | None = None,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("review")),
) -> dict:
    scope = await manager_scope(p)
    off = _parse_offset(offset)
    total = await get_pending_submissions_count(city_scope=scope)
    rows = await get_pending_submissions(limit=1, offset=off, city_scope=scope)
    if not rows:
        # Пустая очередь — remaining 0; все карточки пропущены — remaining = total,
        # фронт предлагает «начать сначала».
        return {"empty": True, "remaining": total, "offset": off}
    row = rows[0]
    parts = await get_submission_parts_or_legacy(row)

    attempt = None
    limit = await get_setting_typed("game_resubmit_limit")
    if limit:
        rejected = await count_rejected_submissions(row["task_id"], row["user_id"])
        attempt = {"k": rejected + 1, "n": limit}

    delegate_city = None
    if await cities_module_on():
        delegate_city = await city_label(normalize_city(row.get("user_event_city")))

    submitted_at, deadline_at = row.get("submitted_at"), row.get("task_deadline_at")
    after_deadline = bool(submitted_at and deadline_at and str(submitted_at) > str(deadline_at))

    return {
        "submission": {
            "id": row["id"],
            "task_id": row["task_id"],
            "user_id": row["user_id"],
            "submitted_at": submitted_at,
        },
        "task": {
            "id": row["task_id"],
            "title": task_title({"title": row.get("task_title"), "text": row.get("task_text")}),
            "text": row.get("task_text"),
            "category": row.get("task_category"),
            "category_label": await category_label(row.get("task_category")),
            "coins": row.get("task_coins"),
            "proof_label": await proof_types_label(row.get("task_proof_type")),
            "deadline_at": deadline_at,
        },
        "delegate": {
            "name": row.get("user_full_name"),
            "username": row.get("user_username"),
            "city": delegate_city,
        },
        "parts": [
            {"kind": part.get("kind"), "content": part.get("content"), "caption": part.get("caption")}
            for part in parts
        ],
        "attempt": attempt,
        "after_deadline": after_deadline,
        "archived_task": bool(row.get("task_archived_at")),
        "remaining": total,
        "offset": off,
        "position": off + 1,
    }


# ── POST /app/api/review/{sid}/approve ───────────────────────────────────────────────────

class ApproveIn(BaseModel):
    coins: int | None = None


class RejectIn(BaseModel):
    reason: str = ""


async def _notify_delegate(cfg, user_id: int, text: str) -> None:
    """Как бот: ошибка уведомления — в лог, решение уже принято и не откатывается."""
    try:
        await telegram_api.send_message(cfg, user_id, text)
    except TelegramApiError as exc:
        logger.error("review: не удалось уведомить делегата %s (%s)", user_id, exc.reason)


@router.post("/app/api/review/{sid}/approve")
async def review_approve(
    sid: int, request: Request, body: ApproveIn | None = None,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("review")),
) -> dict:
    submission, task = await _load_for_decision(p, sid)
    coins = body.coins if body is not None and body.coins is not None else task["coins"]
    if body is not None and body.coins is not None and not (COINS_MIN <= coins <= COINS_MAX):
        raise HTTPException(400, {"reason": "bad_coins", "text": BAD_COINS_TEXT})

    # T-19-27: та же переменная `coins` уходит и в claim_submission, и в add_coins.
    won = await claim_submission(sid, p.telegram_id, "approved", coins_awarded=coins)
    if not won:
        return {"ok": False, "reason": "already"}

    await add_coins(
        submission["user_id"], coins,
        reason=f"Задание: {str(task['text'])[:60]}",
        changed_by=p.telegram_id,
        source="task",
    )
    await enqueue("submission_reviewed", {
        "submission_id": sid,
        "user_id": submission["user_id"],
        "status": "approved",
        "coins": coins,
    })
    await _notify_delegate(
        request.app.state.cfg, submission["user_id"],
        f"✅ Задание «{task['text']}» одобрено! +{coins}🪙",
    )
    return {"ok": True, "status": "approved", "coins": coins}


# ── POST /app/api/review/{sid}/reject ────────────────────────────────────────────────────

@router.post("/app/api/review/{sid}/reject")
async def review_reject(
    sid: int, request: Request, body: RejectIn | None = None,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("review")),
) -> dict:
    reason = (body.reason if body is not None else "").strip()[:REASON_MAX]
    if not reason:
        raise HTTPException(400, {"reason": "reason_required", "text": REASON_REQUIRED_TEXT})
    submission, task = await _load_for_decision(p, sid)

    # Никакого add_coins в этой ветке — ни при каком исходе.
    won = await claim_submission(sid, p.telegram_id, "rejected", reject_reason=reason)
    if not won:
        return {"ok": False, "reason": "already"}

    await enqueue("submission_reviewed", {
        "submission_id": sid,
        "user_id": submission["user_id"],
        "status": "rejected",
        "coins": 0,
    })
    await _notify_delegate(
        request.app.state.cfg, submission["user_id"],
        f"❌ Задание «{task['text']}» отклонено.\n\nПричина: {reason}",
    )
    return {"ok": True, "status": "rejected"}
