"""Quick 260906-8uq (FAQ-01..06): раздел «❓ Частые вопросы» в Mini App.

`GET /app/api/faq` (задача 5) — делегатский список. Правило видимости пункта («городской
пункт перекрывает общий») живёт ОДИН раз в `services/faq.py::apply_city_overrides` — здесь
второй копии нет, только `list_faq_for_city` + применение правила (тот же приём, что
`handlers/user_actions.py::_faq_visible_items` использует на стороне бота).

Город делегата: `Principal.city` — привязка СОТРУДНИКА (`staff_city`), у делегата она всегда
`None`, поэтому город читаем из его собственной строки `users.event_city` — тот же fail-soft
приём, что `handlers.user_actions._delegate_city_for_faq` (ошибка чтения не роняет экран,
только сужает список до общих пунктов).

`POST /app/api/faq` (задача 6) — менеджерская симметрия кнопки «❓ В FAQ» из журнала вопросов
(`handlers/admin_faq.py::afaq_save_draft`), но МИНИМАЛЬНО: тело `{question, answer}`, город —
`Principal.city` (привязка МЕНЕДЖЕРА, не делегата — другая сторона той же дихотомии, что и у
`_delegate_city` выше), пусто -> общий пункт. Полноценного экрана управления FAQ в приложении
этот квик не заводит (`not_in_scope` плана) — только точка входа, ведение списка остаётся в
боте. Дубль ищем той же дедупликацией, что и `afaq_save_draft`: по нормализованному вопросу
СРЕДИ пунктов ТОГО ЖЕ городского ведра (`city == target_city`, точное совпадение, не
делегатская видимость `apply_city_overrides`)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cities import cities_module_on, normalize_city
from database.db import create_faq_item, get_user, list_faq_for_city, list_faq_items
from services.faq import apply_city_overrides, normalize_question
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_cap, require_section

router = APIRouter()

QUESTION_MAX = 300
ANSWER_MAX = 4000

EMPTY_QUESTION_TEXT = "Вопрос не может быть пустым — напишите текст вопроса."
EMPTY_ANSWER_TEXT = "Ответ не может быть пустым — напишите текст ответа."
QUESTION_TOO_LONG_TEXT = f"Вопрос длиннее {QUESTION_MAX} символов — сократите текст."
ANSWER_TOO_LONG_TEXT = f"Ответ длиннее {ANSWER_MAX} символов — сократите текст."


async def _delegate_city(p: Principal) -> str | None:
    if not await cities_module_on():
        return None
    try:
        user = await get_user(p.telegram_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception:
        return None


@router.get("/app/api/faq")
async def faq_list(
    p: Principal = Depends(delegate_gate),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    city = await _delegate_city(p)
    try:
        rows = await list_faq_for_city(city)
    except Exception:
        rows = []
    items = apply_city_overrides(rows, city)
    return {
        "items": [
            {"id": row["id"], "question": row["question"], "answer": row["answer"]}
            for row in items
        ],
        "empty_text": await get_setting_typed("faq_empty_text"),
    }


class FaqIn(BaseModel):
    question: str = ""
    answer: str = ""


@router.post("/app/api/faq")
async def faq_create(
    body: FaqIn,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("questions")),
) -> dict:
    question = body.question.strip()
    answer = body.answer.strip()
    if not question:
        raise HTTPException(400, {"reason": "empty", "text": EMPTY_QUESTION_TEXT})
    if not answer:
        raise HTTPException(400, {"reason": "empty", "text": EMPTY_ANSWER_TEXT})
    if len(question) > QUESTION_MAX:
        raise HTTPException(400, {"reason": "too_long", "text": QUESTION_TOO_LONG_TEXT})
    if len(answer) > ANSWER_MAX:
        raise HTTPException(400, {"reason": "too_long", "text": ANSWER_TOO_LONG_TEXT})

    target_city = p.city or None

    # Дубль — среди пунктов ТОГО ЖЕ городского ведра, точное совпадение (не делегатская
    # видимость `apply_city_overrides`), та же дедупликация, что `afaq_save_draft` в боте.
    all_items = await list_faq_items(city_scope=None)
    norm_target = normalize_question(question)
    for row in all_items:
        if row.get("city") != target_city:
            continue
        if normalize_question(row.get("question")) == norm_target:
            return {"ok": False, "reason": "already", "id": row["id"]}

    new_id = await create_faq_item(
        city=target_city, question=question, answer=answer, created_by=p.telegram_id,
    )
    return {"ok": True, "id": new_id}


__all__ = ["router"]
