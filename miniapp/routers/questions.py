"""Quick 260904-2cj (QJRN-01..04): журнал вопросов делегатов в Mini App — та же поверхность,
что «❓ Вопросы делегатов» в чате бота (`handlers/admin_questions.py`, импортировать нельзя —
aiogram), правило статуса и постраничная выборка — ОБЩИЕ (`services/questions.py`,
`database.db.list_questions_page`/`count_questions_by_status`), второй копии правила здесь нет.

Порядок ответа — `POST /{qid}/answer`:

    text пуст/длиннее ANSWER_MAX -> 400, без обращения к Bot API
    get_question -> 404 not_found
    уже delivered -> {ok: false, reason: "already", by}
    claim_question(...) -> проиграл и захватил НЕ ты -> тот же "already"
        (проиграл, но захват твой же, доставка не прошла в прошлый раз — тот же приём, что
        T-08-33 часть C / `handlers/admin.py::admin_reply_to_question`: это retry, не чужой
        ответ)
    -> telegram_api.send_message(...) -> ТОЛЬКО при успехе set_question_answer(...)
    -> {ok: true, status: "answered"}

Сбой доставки (`TelegramApiError`) не откатывает захват — то же принятое ограничение, что у
бота (см. `handlers/admin.py::_attempt_question_delivery`'s docstring): повторная отправка
без этого ограничения могла бы задвоить сообщение делегату.

Уведомление остальных держателей `moderate_reg` («кто ответил», D-13/`_notify_other_moderate_
reg_holders`) — aiogram-путь бота, из веба НЕ шлётся (документировано как ограничение квика
в SUMMARY, не в коде — иначе создавать здесь второй `outbox`-тип ради одного квика лишнее)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from cities import cities_module_on, city_label, normalize_city
from database.db import (
    claim_question,
    count_questions_by_status,
    get_question,
    get_user,
    list_questions_page,
    set_question_answer,
)
from services import applications
from services.questions import FILTER_LABELS, STATUSES, format_stamp, is_stuck, question_status, status_label
from settings_schema import get_setting_typed

from miniapp import telegram_api
from miniapp.deps import Principal, require_cap, require_section
from miniapp.telegram_api import TelegramApiError

logger = logging.getLogger(__name__)
router = APIRouter()

LIMIT_DEFAULT = 20
LIMIT_MAX = 50
ANSWER_MAX = 4000

EMPTY_TEXT_TEXT = "Ответ не может быть пустым — напишите текст ответа."
TOO_LONG_TEXT = f"Ответ длиннее {ANSWER_MAX} символов — сократите текст."
DELIVERY_FAILED_TEXT = (
    "Не удалось доставить ответ — делегат мог заблокировать бота или произошла временная "
    "ошибка. Попробуйте ещё раз позже."
)


def _parse_int(raw, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


async def _manager_name(p: Principal) -> str:
    user = await get_user(p.telegram_id)
    full_name = (user or {}).get("full_name")
    if full_name:
        return str(full_name)
    if p.username:
        return f"@{p.username}"
    return f"Менеджер {p.telegram_id}"


async def _item(row: dict, module_on: bool, city_labels: dict[str, str]) -> dict:
    status = question_status(row)
    city = None
    if module_on:
        code = normalize_city(row.get("user_event_city"))
        if code not in city_labels:
            city_labels[code] = await city_label(code)
        city = city_labels[code]
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row.get("user_full_name"),
        "username": row.get("user_username"),
        "city": city,
        "asked_at": format_stamp(row.get("asked_at")),
        "question_text": row.get("question_text"),
        "status": status,
        "status_label": status_label(row),
        "stuck": is_stuck(row),
        "answered_by_name": row.get("answered_by_name"),
        "answered_at": format_stamp(row.get("answered_at")) if row.get("answered_at") else None,
        "answer_text": row.get("answer_text"),
        "can_answer": status != "answered",
    }


# ── GET /app/api/questions ──────────────────────────────────────────────────────────────

@router.get("/app/api/questions")
async def questions_list(
    status: str | None = None,
    offset: str | None = None,
    limit: str | None = None,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("questions")),
) -> dict:
    off = _parse_int(offset, 0, 0, 10_000_000)
    lim = _parse_int(limit, LIMIT_DEFAULT, 1, LIMIT_MAX)
    # Неизвестный статус — чип экрана, а не контракт (тот же приём, что `track_filter` в
    # miniapp/routers/applications.py): трактуем как «все», не 400.
    status_filter = status if status in STATUSES else None

    scope = await applications.manager_scope(p.city)
    counts = await count_questions_by_status(city_scope=scope)
    rows = await list_questions_page(status=status_filter, city_scope=scope, limit=lim, offset=off)

    module_on = await cities_module_on()
    city_labels: dict[str, str] = {}
    items = [await _item(row, module_on, city_labels) for row in rows]

    total = counts[status_filter] if status_filter else counts["all"]
    filters = [
        {"key": key, "label": FILTER_LABELS[key], "count": counts["all"] if key == "all" else counts[key]}
        for key in ("all", *STATUSES)
    ]

    return {
        "items": items,
        "total": total,
        "counts": counts,
        "offset": off,
        "limit": lim,
        "filters": filters,
        "empty_text": await get_setting_typed("miniapp_empty_questions"),
        "answer_button": await get_setting_typed("miniapp_questions_answer_button"),
        "sent_toast": await get_setting_typed("miniapp_questions_sent_toast"),
    }


# ── POST /app/api/questions/{qid}/answer ─────────────────────────────────────────────────

class AnswerIn(BaseModel):
    text: str = ""


@router.post("/app/api/questions/{qid}/answer")
async def questions_answer(
    qid: int,
    body: AnswerIn,
    request: Request,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("questions")),
) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, {"reason": "empty_text", "text": EMPTY_TEXT_TEXT})
    if len(text) > ANSWER_MAX:
        raise HTTPException(400, {"reason": "too_long", "text": TOO_LONG_TEXT})

    row = await get_question(qid)
    if row is None:
        raise HTTPException(404, {"reason": "not_found"})
    if row.get("delivered_at"):
        return {"ok": False, "reason": "already", "by": row.get("answered_by_name")}

    manager_name = await _manager_name(p)
    won = await claim_question(qid, p.telegram_id, manager_name)
    if not won:
        row2 = await get_question(qid)
        is_own_retry = bool(
            row2 and row2.get("answered_by") == p.telegram_id and not row2.get("delivered_at")
        )
        if not is_own_retry:
            return {"ok": False, "reason": "already", "by": (row2 or {}).get("answered_by_name")}

    try:
        await telegram_api.send_message(
            request.app.state.cfg, row["user_id"], f"💬 Ответ от организаторов:\n\n{text}",
        )
    except TelegramApiError as exc:
        logger.error("questions: не удалось доставить ответ %s (%s)", qid, exc.reason)
        return {"ok": False, "reason": "delivery_failed", "text": DELIVERY_FAILED_TEXT}

    await set_question_answer(qid, text)
    return {"ok": True, "status": "answered"}


__all__ = ["router"]
